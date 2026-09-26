"""投递记录的数据访问层。

并发性正确性的核心：所有状态迁移都用 *单条带 WHERE 条件的 UPDATE ... RETURNING*
完成。PostgreSQL 会用行锁串行化对同一行的并发更新：
- 第一个满足条件的事务把 status 从 active 改走并返回行（赢家）；
- 其它事务的 WHERE 不再匹配，返回空（输家），随后读取当前终态给出明确结果。

审计哈希链的追加使用同样的裁决模式：对单行 ``audit_chain_state`` 做
``UPDATE ... WHERE last_seq=:expected`` 的条件更新，竞争失败的事务重试，
因此链序号的全序由数据库（而非应用层锁）决定。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterable
from datetime import datetime

import sqlalchemy.exc
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .audit_chain import (
    GENESIS_HASH,
    ChainEntry,
    chain_hash,
    event_digest_for,
)
from .models import AuditChainState, AuditLog, Delivery, DropRequest
from .utils import utcnow

# 可能的结果状态
CONSUMED = "consumed"
REVOKED = "revoked"
EXPIRED = "expired"
ACTIVE = "active"
UNKNOWN = "unknown"

# 盲投递请求状态机：awaiting -> submitted -> consumed；
# 活状态（awaiting/submitted）可被撤回或过期裁决为 revoked / expired
AWAITING = "awaiting"
SUBMITTED = "submitted"
# pending 仅是取件响应结果（空槽尚未被提交），不落库、不入终态缓存
PENDING = "pending"

_DELIVERY_COLS = (
    Delivery.id,
    Delivery.status,
    Delivery.content,
    Delivery.kind,
    Delivery.filename,
    Delivery.content_type,
    Delivery.content_size,
    Delivery.content_sha256,
    Delivery.expires_at,
    Delivery.consumed_at,
    Delivery.revoked_at,
)

# 导出/校验链记录时的列（绝不包含 deliveries.content 等明文投递内容）
_CHAIN_COLS = (
    AuditLog.seq,
    AuditLog.created_at,
    AuditLog.action,
    AuditLog.result,
    AuditLog.delivery_id,
    AuditLog.token_hash,
    AuditLog.client_ip,
    AuditLog.user_agent,
    AuditLog.detail,
    AuditLog.event_digest,
    AuditLog.prev_hash,
    AuditLog.entry_hash,
)

# 链追加的竞争重试上限（正常竞争下通常 1~2 次即完成）
_MAX_CHAIN_ATTEMPTS = 200

# 盲投槽取件赢家需要带回的列（含一次性取出的明文内容）
_DROP_REQUEST_COLS = (
    DropRequest.id,
    DropRequest.status,
    DropRequest.kind,
    DropRequest.content,
    DropRequest.filename,
    DropRequest.content_type,
    DropRequest.content_size,
    DropRequest.content_sha256,
    DropRequest.expires_at,
    DropRequest.submitted_at,
    DropRequest.consumed_at,
    DropRequest.revoked_at,
)

# 仍可被提交（仅 awaiting）/ 撤回 / 过期裁决的"活"状态
_DROP_LIVE_STATES = (AWAITING, SUBMITTED)


class DeliveryStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        token_hash: str,
        kind: str,
        content: bytes,
        expires_at: datetime,
        filename: str | None = None,
        content_type: str | None = None,
        content_sha256: str,
    ) -> Delivery:
        delivery = Delivery(
            token_hash=token_hash,
            kind=kind,
            content=content,
            filename=filename,
            content_type=content_type,
            content_size=len(content),
            content_sha256=content_sha256,
            expires_at=expires_at,
        )
        self.session.add(delivery)
        await self.session.flush()
        return delivery

    async def _lazily_expire(self, token_hash: str, now: datetime) -> bool:
        """把 active 且已到期的行提升为 expired。

        必须在 *独立事务* 中执行：调用方的读事务可能持有旧快照
        （SQLite/REPEATABLE READ），在其中回写过期状态会覆盖
        并发赢家刚刚提交的 consumed/revoked。
        """
        result = await self.session.execute(
            update(Delivery)
            .where(
                Delivery.token_hash == token_hash,
                Delivery.status == ACTIVE,
                Delivery.expires_at <= now,
            )
            .values(status=EXPIRED, expired_at=now)
        )
        await self.session.commit()
        return (result.rowcount or 0) > 0

    async def claim(self, token_hash: str) -> tuple[bool, str, Delivery | None]:
        """原子消费。

        返回 (won, state, row)：
        - won=True  本次调用是唯一赢家，state=consumed，row 带明文内容
                    （内容仅此一次从事务中取出）。
        - won=False 本次未抢到，state 为明确的失败原因：
                    consumed（已被他人取走）/ revoked / expired / unknown，
                    row 不含内容（由上层按需读取，但不返回给调用方）。

        正确性说明：
        - 条件 UPDATE ... WHERE status='active' AND 未过期 是唯一裁决点。
          PostgreSQL（READ COMMITTED）用行锁串行化并发更新，仅一个事务命中。
        - 未命中即提交，绝不在同一事务里回写过期，避免旧快照覆盖赢家终态。
        """
        now = utcnow()
        result = await self.session.execute(
            update(Delivery)
            .where(
                Delivery.token_hash == token_hash,
                Delivery.status == ACTIVE,
                Delivery.expires_at > now,
            )
            .values(status=CONSUMED, consumed_at=now)
            .returning(*_DELIVERY_COLS)
        )
        row = result.first()
        if row is not None:
            await self.session.commit()
            return True, CONSUMED, row

        # 未命中：结束本事务后，在独立事务里幂等地推进过期，再读终态
        await self.session.commit()
        await self._lazily_expire(token_hash, now)

        current = (
            await self.session.execute(
                select(Delivery).where(Delivery.token_hash == token_hash)
            )
        ).scalar_one_or_none()
        if current is None:
            return False, UNKNOWN, None
        return False, current.status, current

    async def revoke(self, token_hash: str) -> tuple[bool, str, Delivery | None]:
        """原子撤回。返回 (won, state, row)。

        won=True 表示本次成功撤回；won=False 时 state 为
        consumed / expired / unknown（已被取走、已过期、令牌不存在）。
        """
        now = utcnow()
        result = await self.session.execute(
            update(Delivery)
            .where(
                Delivery.token_hash == token_hash,
                Delivery.status == ACTIVE,
                Delivery.expires_at > now,
            )
            .values(status=REVOKED, revoked_at=now)
            .returning(Delivery.id, Delivery.status, Delivery.expires_at, Delivery.revoked_at)
        )
        row = result.first()
        if row is not None:
            await self.session.commit()
            current = (
                await self.session.execute(
                    select(Delivery).where(Delivery.token_hash == token_hash)
                )
            ).scalar_one()
            return True, REVOKED, current

        await self.session.commit()
        await self._lazily_expire(token_hash, now)

        current = (
            await self.session.execute(
                select(Delivery).where(Delivery.token_hash == token_hash)
            )
        ).scalar_one_or_none()
        if current is None:
            return False, UNKNOWN, None
        return False, current.status, current

    async def sweep_expired(self) -> int:
        """后台批量标记过期（惰性过期之外的兜底），返回影响行数。"""
        result = await self.session.execute(
            update(Delivery)
            .where(Delivery.status == ACTIVE, Delivery.expires_at <= utcnow())
            .values(status=EXPIRED, expired_at=utcnow())
        )
        await self.session.commit()
        return result.rowcount or 0

    # ---------------- 审计哈希链 ----------------

    async def write_audit(
        self,
        *,
        action: str,
        result: str,
        delivery_id: uuid.UUID | None = None,
        token_hash: str | None = None,
        detail: dict | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        """追加一条入链审计记录，并在同一事务中推进链游标。

        未知令牌等失败请求也走这里，因此同样入链。

        步骤（顺序是正确性的关键）：

        1. 读取链游标 (last_seq, last_hash)；
        2. 用读到的游标计算新序号、事件摘要与链哈希；
        3. **先**执行条件 UPDATE 推进游标（WHERE last_seq=:read），
           这是唯一裁决点——抢占成功后才 INSERT 审计行，因此竞态时
           不会先撞 seq 唯一索引；
        4. 命中：INSERT 审计行并提交（此时仍持有链游标行锁，序号无争议）；
           未命中：重试。

        并发裁决：

        - PostgreSQL（READ COMMITTED）：条件 UPDATE 在链游标单行上排队，
          持锁者提交后 WHERE 按最新行版本重新判定，只有一个事务命中；
          未命中的事务没有产生任何写入，直接重读游标重试即可，
          同事务中的其它写入（如新建投递的 INSERT）不受影响。
        - SQLite（测试，WAL）：读快照不随提交前进，且读事务首次写入时
          若他人已提交会得到 SQLITE_BUSY_SNAPSHOT。两种失配都以
          整事务回滚 + 重读重试解决；此路径只可能发生在纯审计追加场景
          （create 路径以写 INSERT 开始，拿到写锁后快照必然最新；
          claim/revoke 的状态迁移已提前提交）。
        """
        created_at = utcnow()
        ua = (user_agent or "")[:512] or None
        ip = (client_ip or None)[:64] if client_ip else None
        th = (token_hash or None)[:64] if token_hash else None

        is_sqlite = self.session.bind.dialect.name == "sqlite"

        for _attempt in range(1, _MAX_CHAIN_ATTEMPTS + 1):
            last_seq, last_hash = await self._load_chain_state()
            seq = last_seq + 1
            digest = event_digest_for(
                seq=seq,
                created_at=created_at,
                action=action,
                result=result,
                delivery_id=delivery_id,
                token_hash=th,
                client_ip=ip,
                user_agent=ua,
                detail=detail,
            )
            entry_hash_value = chain_hash(last_hash, digest)

            try:
                # 唯一裁决点：仅当链游标仍停在本事务读到的位置时才推进
                cursor_result = await self.session.execute(
                    update(AuditChainState)
                    .where(
                        AuditChainState.id == 1,
                        AuditChainState.last_seq == last_seq,
                    )
                    .values(
                        last_seq=seq,
                        last_hash=entry_hash_value,
                        updated_at=created_at,
                    )
                )
            except sqlalchemy.exc.OperationalError:
                # SQLite/WAL：读快照过期导致的 SQLITE_BUSY_SNAPSHOT。
                # 整事务回滚以获取新快照（此路径无同事务兄弟写入，见上）。
                if not is_sqlite:
                    raise
                await self.session.rollback()
                await asyncio.sleep(0)
                continue

            if cursor_result.rowcount == 1:
                # 已独占链游标行锁：审计行的 seq 无争议，随外层事务原子提交
                entry = AuditLog(
                    delivery_id=delivery_id,
                    token_hash=th,
                    action=action,
                    result=result,
                    detail=detail,
                    client_ip=ip,
                    user_agent=ua,
                    created_at=created_at,
                    seq=seq,
                    event_digest=digest,
                    prev_hash=last_hash,
                    entry_hash=entry_hash_value,
                )
                self.session.add(entry)
                await self.session.flush()
                await self.session.commit()
                return entry

            # 游标已被他人推进：
            # - PG：本 UPDATE 未产生任何写入，重读（READ COMMITTED 可见新提交）
            #   即可重试，同事务兄弟写入原样保留；
            # - SQLite：快照不会前进，需整事务回滚后重读。
            if is_sqlite:
                await self.session.rollback()
            await asyncio.sleep(0)

        raise RuntimeError(
            f"审计链追加在 {_MAX_CHAIN_ATTEMPTS} 次尝试后仍未成功"
        )

    async def _load_chain_state(self) -> tuple[int, str]:
        """读取链游标；极端情况下（旧库未种子化）就地自愈。"""
        state_row = (
            await self.session.execute(
                select(AuditChainState.last_seq, AuditChainState.last_hash).where(
                    AuditChainState.id == 1
                )
            )
        ).first()
        if state_row is None:
            self.session.add(
                AuditChainState(id=1, last_seq=0, last_hash=GENESIS_HASH)
            )
            await self.session.flush()
            return 0, GENESIS_HASH
        return state_row.last_seq, state_row.last_hash

    async def iter_audit_range(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> AsyncIterator[ChainEntry]:
        """按链序号顺序流式迭代时间范围内的审计记录（只读）。

        时间范围约定：created_at >= start 且 created_at < end（左闭右开）。
        只选择链/元数据列，不触碰任何投递明文内容。
        """
        stmt = select(*_CHAIN_COLS)
        if start is not None:
            stmt = stmt.where(AuditLog.created_at >= start)
        if end is not None:
            stmt = stmt.where(AuditLog.created_at < end)
        stmt = stmt.order_by(AuditLog.seq.asc()).execution_options(stream_results=True)

        result = await self.session.stream(stmt)
        try:
            async for row in result:
                yield ChainEntry.from_row(row)
        finally:
            await result.close()

    async def audit_range_meta(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[int, ChainEntry | None, ChainEntry | None]:
        """返回时间范围内的 (记录数, 首条, 末条)，与流式查询共享同一快照。"""
        filters = []
        if start is not None:
            filters.append(AuditLog.created_at >= start)
        if end is not None:
            filters.append(AuditLog.created_at < end)

        count = await self.session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(*filters)
        )
        if not count:
            return 0, None, None

        first = (
            await self.session.execute(
                self._range_stmt(filters, ascending=True)
            )
        ).first()
        last = (
            await self.session.execute(
                self._range_stmt(filters, ascending=False)
            )
        ).first()
        return int(count or 0), ChainEntry.from_row(first), ChainEntry.from_row(last)

    @staticmethod
    def _range_stmt(filters: Iterable, *, ascending: bool):
        order = AuditLog.seq.asc() if ascending else AuditLog.seq.desc()
        return select(*_CHAIN_COLS).where(*filters).order_by(order).limit(1)

    async def entry_hash_before(self, first_seq: int) -> str | None:
        """供片段校验锚定：返回序号 ``first_seq - 1`` 记录的链哈希。

        库中不存在该记录（例如范围起点之前无记录）时返回 None。
        """
        if first_seq <= 1:
            return GENESIS_HASH
        row = await self.session.scalar(
            select(AuditLog.entry_hash)
            .where(AuditLog.seq == first_seq - 1)
        )
        return row


class DropRequestStore:
    """盲投递请求的数据访问层。

    与 DeliveryStore 完全相同的裁决模式：所有状态迁移都是 *单条带 WHERE
    条件的 UPDATE ... RETURNING*，由数据库行锁保证并发提交、收件人撤回与
    过期三者竞争时只有一个赢家。审计链写入统一走 DeliveryStore.write_audit
    （两端点注入的 store 共享同一 session）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        submit_token_hash: str,
        pickup_token_hash: str,
        expires_at: datetime,
    ) -> DropRequest:
        req = DropRequest(
            submit_token_hash=submit_token_hash,
            pickup_token_hash=pickup_token_hash,
            expires_at=expires_at,
        )
        self.session.add(req)
        await self.session.flush()
        return req

    async def _lazily_expire(self, column, token_hash: str, now: datetime) -> None:
        """把该令牌对应行中已到期的活状态提升为 expired。

        与 DeliveryStore._lazily_expire 一样必须在 *独立事务* 中执行，
        避免旧快照覆盖并发赢家刚提交的终态。
        """
        await self.session.execute(
            update(DropRequest)
            .where(
                column == token_hash,
                DropRequest.status.in_(_DROP_LIVE_STATES),
                DropRequest.expires_at <= now,
            )
            .values(status=EXPIRED, expired_at=now)
        )
        await self.session.commit()

    async def submit(
        self,
        submit_token_hash: str,
        *,
        kind: str,
        content: bytes,
        filename: str | None = None,
        content_type: str | None = None,
        content_sha256: str,
    ) -> tuple[bool, str, DropRequest | None]:
        """原子写入空槽：并发下只有一个提交者成功。

        返回 (won, state, row)：
        - won=True  本次写入成功，槽状态推进为 submitted；
        - won=False state 为明确失败原因：submitted（已被他人提交）/
                    consumed（已提交且已被取件，由上层对提交者映射为
                    submitted，不泄露收件人是否已读）/ revoked / expired /
                    unknown。
        """
        now = utcnow()
        result = await self.session.execute(
            update(DropRequest)
            .where(
                DropRequest.submit_token_hash == submit_token_hash,
                DropRequest.status == AWAITING,
                DropRequest.expires_at > now,
            )
            .values(
                status=SUBMITTED,
                kind=kind,
                content=content,
                filename=filename,
                content_type=content_type,
                content_size=len(content),
                content_sha256=content_sha256,
                submitted_at=now,
            )
            .returning(DropRequest.id, DropRequest.status, DropRequest.submitted_at)
        )
        row = result.first()
        if row is not None:
            await self.session.commit()
            return True, SUBMITTED, row

        await self.session.commit()
        await self._lazily_expire(DropRequest.submit_token_hash, submit_token_hash, now)

        current = (
            await self.session.execute(
                select(DropRequest).where(
                    DropRequest.submit_token_hash == submit_token_hash
                )
            )
        ).scalar_one_or_none()
        if current is None:
            return False, UNKNOWN, None
        return False, current.status, current

    async def claim(self, pickup_token_hash: str) -> tuple[bool, str, DropRequest | None]:
        """原子取件（仅当内容已提交）。返回 (won, state, row)。

        won=False 时 state 为 pending（空槽或极端竞态下未抢到，均提示
        稍后重试，非终态）/ consumed / revoked / expired / unknown。
        """
        now = utcnow()
        result = await self.session.execute(
            update(DropRequest)
            .where(
                DropRequest.pickup_token_hash == pickup_token_hash,
                DropRequest.status == SUBMITTED,
                DropRequest.expires_at > now,
            )
            .values(status=CONSUMED, consumed_at=now)
            .returning(*_DROP_REQUEST_COLS)
        )
        row = result.first()
        if row is not None:
            await self.session.commit()
            return True, CONSUMED, row

        await self.session.commit()
        await self._lazily_expire(DropRequest.pickup_token_hash, pickup_token_hash, now)

        current = (
            await self.session.execute(
                select(DropRequest).where(
                    DropRequest.pickup_token_hash == pickup_token_hash
                )
            )
        ).scalar_one_or_none()
        if current is None:
            return False, UNKNOWN, None
        if current.status in _DROP_LIVE_STATES:
            # awaiting：空槽等待提交；submitted：本次 UPDATE 后他人才提交
            # 成功的极端竞态。两者都让收件人稍后重试即可。
            return False, PENDING, current
        return False, current.status, current

    async def revoke(self, pickup_token_hash: str) -> tuple[bool, str, DropRequest | None]:
        """收件人原子撤回（awaiting / submitted 均可撤）。返回 (won, state, row)。

        won=False 时 state 为 consumed / revoked（幂等场景，Redis 标记
        缺失时由上层转为 already_revoked）/ expired / unknown。
        """
        now = utcnow()
        result = await self.session.execute(
            update(DropRequest)
            .where(
                DropRequest.pickup_token_hash == pickup_token_hash,
                DropRequest.status.in_(_DROP_LIVE_STATES),
                DropRequest.expires_at > now,
            )
            .values(status=REVOKED, revoked_at=now)
            .returning(
                DropRequest.id,
                DropRequest.status,
                DropRequest.revoked_at,
                DropRequest.submit_token_hash,
            )
        )
        row = result.first()
        if row is not None:
            await self.session.commit()
            return True, REVOKED, row

        await self.session.commit()
        await self._lazily_expire(DropRequest.pickup_token_hash, pickup_token_hash, now)

        current = (
            await self.session.execute(
                select(DropRequest).where(
                    DropRequest.pickup_token_hash == pickup_token_hash
                )
            )
        ).scalar_one_or_none()
        if current is None:
            return False, UNKNOWN, None
        return False, current.status, current

    async def sweep_expired(self) -> int:
        """后台批量把到期的 awaiting/submitted 槽标记为 expired。"""
        now = utcnow()
        result = await self.session.execute(
            update(DropRequest)
            .where(
                DropRequest.status.in_(_DROP_LIVE_STATES),
                DropRequest.expires_at <= now,
            )
            .values(status=EXPIRED, expired_at=now)
        )
        await self.session.commit()
        return result.rowcount or 0
