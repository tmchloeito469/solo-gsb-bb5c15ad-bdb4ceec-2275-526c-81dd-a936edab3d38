"""盲投递请求：空槽创建、一次性提交、pending 取件、撤回、过期、权限隔离、并发与审计。"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from app.models import AuditLog, DropRequest
from app.utils import hash_token

pytestmark = pytest.mark.asyncio


async def _create_slot(client: AsyncClient, ttl: int = 3600) -> dict:
    resp = await client.post("/api/v1/drop-requests", json={"ttl_seconds": ttl})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _submit_text(client: AsyncClient, token: str, text: str = "盲投内容"):
    return await client.post(
        f"/api/v1/drop-requests/submit/{token}", json={"text": text}
    )


async def _expire_slot(db, request_id) -> None:
    """直接在数据库里把到期时间挪到过去，模拟已过期。"""
    from datetime import datetime, timedelta, timezone

    async with db.session_factory() as session:
        row = await session.get(DropRequest, request_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await session.commit()


# ---------------- 创建空槽 ----------------


async def test_create_slot_returns_two_distinct_tokens(client: AsyncClient):
    data = await _create_slot(client)
    assert data["request_id"]
    assert data["expires_at"]
    assert data["submit_token"] != data["pickup_token"]
    assert len(data["submit_token"]) >= 40
    assert len(data["pickup_token"]) >= 40
    assert data["submit_url"].endswith(
        f"/api/v1/drop-requests/submit/{data['submit_token']}"
    )
    assert data["pickup_url"].endswith(
        f"/api/v1/drop-requests/pickup/{data['pickup_token']}"
    )


async def test_create_slot_validates_ttl(client: AsyncClient):
    resp = await client.post("/api/v1/drop-requests", json={"ttl_seconds": 99999999})
    assert resp.status_code == 422


async def test_token_hashes_stored_separately(client: AsyncClient, db):
    slot = await _create_slot(client)
    async with db.session_factory() as session:
        row = await session.get(DropRequest, slot["request_id"])
        # 两类令牌分别哈希存储，库中无明文
        assert row.submit_token_hash == hash_token(slot["submit_token"])
        assert row.pickup_token_hash == hash_token(slot["pickup_token"])
        assert row.submit_token_hash != row.pickup_token_hash
        assert slot["submit_token"] not in (row.submit_token_hash, row.pickup_token_hash)
        assert slot["pickup_token"] not in (row.submit_token_hash, row.pickup_token_hash)
        assert row.status == "awaiting"
        assert row.content is None


# ---------------- 提交 → 取件 完整链路 ----------------


async def test_submit_then_pickup_roundtrip(client: AsyncClient):
    slot = await _create_slot(client)

    # 提交前取件：pending（非终态，可重试）
    pending = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert pending.status_code == 202
    assert pending.json()["result"] == "pending"

    submitted = await _submit_text(client, slot["submit_token"], "给收件人的秘密")
    assert submitted.status_code == 201
    assert submitted.json()["result"] == "success"
    assert submitted.json()["request_id"] == slot["request_id"]
    assert submitted.json()["submitted_at"]

    first = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert first.status_code == 200
    body = first.json()
    assert body["result"] == "success"
    assert body["content"] == "给收件人的秘密"
    assert body["content_sha256"]
    assert body["consumed_at"]

    # 原子单次取件：第二次起 410 consumed
    second = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert second.status_code == 410
    assert second.json()["result"] == "consumed"


async def test_submit_only_once_same_submitter(client: AsyncClient):
    slot = await _create_slot(client)
    first = await _submit_text(client, slot["submit_token"], "第一份")
    assert first.status_code == 201

    again = await _submit_text(client, slot["submit_token"], "第二份")
    assert again.status_code == 409
    assert again.json()["result"] == "submitted"

    # 槽内仍是第一份内容
    picked = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert picked.status_code == 200
    assert picked.json()["content"] == "第一份"


async def test_concurrent_submit_single_winner(client: AsyncClient, db):
    slot = await _create_slot(client)
    token = slot["submit_token"]

    async def submit(i):
        async with AsyncClient(transport=client._transport, base_url="http://test") as ac:
            return await ac.post(
                f"/api/v1/drop-requests/submit/{token}", json={"text": f"内容-{i}"}
            )

    results = await asyncio.gather(*[submit(i) for i in range(50)])
    winners = [r for r in results if r.status_code == 201]
    losers = [r for r in results if r.status_code == 409]

    assert len(winners) == 1
    assert len(losers) == 49
    assert all(r.json()["result"] == "submitted" for r in losers)

    async with db.session_factory() as session:
        row = await session.get(DropRequest, slot["request_id"])
        assert row.status == "submitted"
        assert row.content is not None
        assert row.submitted_at is not None


async def test_concurrent_pickup_after_submit_single_winner(client: AsyncClient):
    slot = await _create_slot(client)
    await _submit_text(client, slot["submit_token"], "并发取件内容")
    token = slot["pickup_token"]

    async def pick():
        async with AsyncClient(transport=client._transport, base_url="http://test") as ac:
            return await ac.post(f"/api/v1/drop-requests/pickup/{token}")

    results = await asyncio.gather(*[pick() for _ in range(30)])
    success = [r for r in results if r.status_code == 200]
    gone = [r for r in results if r.status_code == 410]
    assert len(success) == 1
    assert success[0].json()["content"] == "并发取件内容"
    assert len(gone) == 29
    assert all(r.json()["result"] == "consumed" for r in gone)


# ---------------- 权限隔离：两类令牌不可互换 ----------------


async def test_tokens_are_not_interchangeable(client: AsyncClient):
    slot = await _create_slot(client)

    # 提交令牌不能取件、不能撤回
    resp = await client.post(f"/api/v1/drop-requests/pickup/{slot['submit_token']}")
    assert resp.status_code == 404
    assert resp.json()["result"] == "unknown"
    resp = await client.post(f"/api/v1/drop-requests/revoke/{slot['submit_token']}")
    assert resp.status_code == 404
    assert resp.json()["result"] == "unknown"

    # 取件令牌不能提交
    resp = await _submit_text(client, slot["pickup_token"])
    assert resp.status_code == 404
    assert resp.json()["result"] == "unknown"

    # 盲投令牌与普通投递接口也不通用
    resp = await client.post(f"/api/v1/pickup/{slot['pickup_token']}")
    assert resp.status_code == 404
    resp = await client.post(f"/api/v1/pickup/{slot['submit_token']}")
    assert resp.status_code == 404

    # 交叉尝试不影响槽状态：正常提交仍可用
    ok = await _submit_text(client, slot["submit_token"], "未被污染")
    assert ok.status_code == 201


# ---------------- 撤回 ----------------


async def test_revoke_awaiting_slot_blocks_submit(client: AsyncClient):
    slot = await _create_slot(client)
    revoked = await client.post(f"/api/v1/drop-requests/revoke/{slot['pickup_token']}")
    assert revoked.status_code == 200
    assert revoked.json()["result"] == "revoked"
    assert revoked.json()["request_id"] == slot["request_id"]

    submit = await _submit_text(client, slot["submit_token"])
    assert submit.status_code == 409
    assert submit.json()["result"] == "revoked"

    pickup = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert pickup.status_code == 409
    assert pickup.json()["result"] == "revoked"


async def test_revoke_after_submit_blocks_pickup(client: AsyncClient):
    slot = await _create_slot(client)
    await _submit_text(client, slot["submit_token"])

    revoked = await client.post(f"/api/v1/drop-requests/revoke/{slot['pickup_token']}")
    assert revoked.status_code == 200
    assert revoked.json()["result"] == "revoked"

    pickup = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert pickup.status_code == 409
    assert pickup.json()["result"] == "revoked"


async def test_revoke_after_consume_is_410(client: AsyncClient):
    slot = await _create_slot(client)
    await _submit_text(client, slot["submit_token"])
    await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")

    resp = await client.post(f"/api/v1/drop-requests/revoke/{slot['pickup_token']}")
    assert resp.status_code == 410
    assert resp.json()["result"] == "consumed"


async def test_revoke_is_idempotent(client: AsyncClient):
    slot = await _create_slot(client)
    first = await client.post(f"/api/v1/drop-requests/revoke/{slot['pickup_token']}")
    assert first.json()["result"] == "revoked"

    second = await client.post(f"/api/v1/drop-requests/revoke/{slot['pickup_token']}")
    assert second.status_code == 200
    assert second.json()["result"] == "already_revoked"


async def test_concurrent_submit_and_revoke_is_consistent(client: AsyncClient):
    """提交与撤回并发：撤回必赢或双赢，提交要么 201 要么 409 revoked，终态一致。"""
    slot = await _create_slot(client)

    async def post(path, **kwargs):
        async with AsyncClient(transport=client._transport, base_url="http://test") as ac:
            return await ac.post(path, **kwargs)

    submit_task = post(
        f"/api/v1/drop-requests/submit/{slot['submit_token']}",
        json={"text": "竞争内容"},
    )
    revoke_task = post(f"/api/v1/drop-requests/revoke/{slot['pickup_token']}")
    submit_resp, revoke_resp = await asyncio.gather(submit_task, revoke_task)

    # 撤回针对 awaiting/submitted 均可，必然成功
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["result"] == "revoked"
    # 提交要么先于撤回写入（201），要么被撤回裁决拒绝（409 revoked）
    assert submit_resp.status_code in (201, 409)
    if submit_resp.status_code == 409:
        assert submit_resp.json()["result"] == "revoked"

    # 无论哪种结局，终态都是 revoked：取件被拒
    pickup = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert pickup.status_code == 409
    assert pickup.json()["result"] == "revoked"


# ---------------- 过期 ----------------


async def test_submit_to_expired_slot_is_410(client: AsyncClient, db):
    slot = await _create_slot(client, ttl=1)
    await _expire_slot(db, slot["request_id"])

    resp = await _submit_text(client, slot["submit_token"])
    assert resp.status_code == 410
    assert resp.json()["result"] == "expired"

    async with db.session_factory() as session:
        row = await session.get(DropRequest, slot["request_id"])
        assert row.status == "expired"
        assert row.expired_at is not None


async def test_pickup_expired_awaiting_slot_is_410_not_pending(client: AsyncClient, db):
    slot = await _create_slot(client, ttl=1)
    await _expire_slot(db, slot["request_id"])

    resp = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert resp.status_code == 410
    assert resp.json()["result"] == "expired"


async def test_revoke_expired_slot_is_410(client: AsyncClient, db):
    slot = await _create_slot(client, ttl=1)
    await _expire_slot(db, slot["request_id"])

    resp = await client.post(f"/api/v1/drop-requests/revoke/{slot['pickup_token']}")
    assert resp.status_code == 410
    assert resp.json()["result"] == "expired"


async def test_sweep_marks_drop_requests_expired(db):
    from datetime import datetime, timedelta, timezone

    from app.store import DropRequestStore
    from app.utils import new_token

    async with db.session_factory() as session:
        _s, sh = new_token()
        _p, ph = new_token()
        await DropRequestStore(session).create(
            submit_token_hash=sh,
            pickup_token_hash=ph,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        await session.commit()
        count = await DropRequestStore(session).sweep_expired()
        assert count == 1


# ---------------- 文件提交 ----------------


async def test_file_submit_and_pickup_once(client: AsyncClient):
    slot = await _create_slot(client)
    payload = b"\x89PNG blind-drop" * 10
    files = {"file": ("机密.png", payload, "image/png")}
    submitted = await client.post(
        f"/api/v1/drop-requests/submit-file/{slot['submit_token']}", files=files
    )
    assert submitted.status_code == 201, submitted.text

    first = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert first.status_code == 200
    assert first.content == payload
    assert first.headers["x-drop-result"] == "success"
    assert "attachment" in first.headers["content-disposition"]
    assert "%E6%9C%BA%E5%AF%86" in first.headers["content-disposition"]
    assert first.headers["x-content-sha256"]

    second = await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")
    assert second.status_code == 410
    assert second.json()["result"] == "consumed"


async def test_submit_oversize_text_is_413(client: AsyncClient):
    slot = await _create_slot(client)
    resp = await client.post(
        f"/api/v1/drop-requests/submit/{slot['submit_token']}",
        json={"text": "x" * 2048},  # 测试上限 1024 字节
    )
    assert resp.status_code == 413


async def test_submit_oversize_file_is_413(client: AsyncClient):
    slot = await _create_slot(client)
    files = {"file": ("big.bin", b"x" * 2048, "application/octet-stream")}
    resp = await client.post(
        f"/api/v1/drop-requests/submit-file/{slot['submit_token']}", files=files
    )
    assert resp.status_code == 413


# ---------------- 审计 ----------------


async def test_drop_request_audit_trail_and_chain(client: AsyncClient, db):
    slot = await _create_slot(client)
    await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")  # pending
    await _submit_text(client, slot["submit_token"])  # success
    await _submit_text(client, slot["submit_token"])  # submitted（重复提交被拒）
    await client.post(f"/api/v1/drop-requests/pickup/{slot['pickup_token']}")  # success
    await client.post(f"/api/v1/drop-requests/revoke/{slot['pickup_token']}")  # consumed

    async with db.session_factory() as session:
        from sqlalchemy import select

        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.seq)))
            .scalars()
            .all()
        )

    actions = [(r.action, r.result) for r in rows]
    assert ("drop_create", "success") in actions
    assert ("drop_pickup", "pending") in actions
    assert ("drop_submit", "success") in actions
    assert ("drop_submit", "submitted") in actions
    assert ("drop_pickup", "success") in actions
    assert ("drop_revoke", "consumed") in actions

    # request_id 记录在 detail 中；delivery_id 为空（盲投槽不是 deliveries 行）
    create_entry = next(r for r in rows if r.action == "drop_create")
    assert create_entry.delivery_id is None
    assert create_entry.detail["request_id"] == slot["request_id"]
    assert create_entry.detail["submit_token_hash"] == hash_token(slot["submit_token"])

    # 全部动作入链后链依然完整
    verify = await client.get("/api/v1/audit/verify")
    assert verify.status_code == 200
    assert verify.json()["ok"] is True
