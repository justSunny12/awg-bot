"""upd.py — команда набора обновлений на стороне шлюза: `upd`, `upd_commit`,
`upd_revert` с сервера и ответ `upd_ack`.

Проверки, ступень и ход — в домене (domain/gateway/upd.py); здесь — когда:
занят — ждём факта не дольше 10 минут; принято — случайная пауза 20–120 с
(шлюзы одного сервера не стартуют синхронно), затем апдейтер. Новый процесс
доводит команду по первому слову сервера (`_upd_resume`): ответ «done» —
только после того, как сторож обновления снят (удержание прежнего на месте).
Ответ, не ушедший по каналу, ждёт следующей сессии.
"""
from __future__ import annotations

import asyncio

from awgbot.runtime import linkclient as pkg          # крючки и задержки — на пакете: их подменяют тесты
from awgbot.util import gwlink

from .common import log

BUSY_POLL_S = 15
PAUSE_S = (20, 120)
SETTLE_POLL_S = 5
SETTLE_MAX_S = 11 * 60          # сторож — 10 минут: дольше ждать нечего


class UpdMixin:
    async def flush_upd_ack(self) -> bool:
        pending = await asyncio.to_thread(self.services.upd_ack_pending)
        if not pending or self._writer is None:
            return False
        if not await self._send("upd_ack", pending, pad=gwlink.PAD_DELTA):
            return False
        await asyncio.to_thread(self.services.upd_ack_clear)
        return True

    async def _upd_notify(self, what: str) -> None:
        """Строка в чат бота шлюза без звука: неудача обновления, откат набора."""
        if pkg._notify is None:
            return
        from awgbot.domain.notices import fleet as notes_
        cmd = await asyncio.to_thread(self.services.upd_cmd)
        if what == "failed":
            bot = await asyncio.to_thread(self.services.link_server_bot)
            text = notes_.agent_update_failed(cmd.get("target") or "", cmd.get("prev") or "",
                                              cmd.get("error") or "", bot, state=cmd.get("state") or "",
                                              why=str(cmd.get("why") or ""), tries=int(cmd.get("tries") or 0))
        else:
            text = notes_.agent_reverted(cmd.get("revert_to") or cmd.get("prev") or "", cmd.get("target") or "")
        try:
            await pkg._notify(text, silent=True)
        except Exception as e:                            # noqa: BLE001
            log.warning("набор: строка в чат бота шлюза не ушла: %s", e)

    def _upd_start(self, *, revert: bool = False) -> None:
        task = self.__dict__.get("_upd_task")
        if task is not None and not task.done():
            return
        self.__dict__["_upd_task"] = asyncio.get_running_loop().create_task(self._upd_run(revert=revert))

    async def _upd_in(self, msg: dict) -> None:
        res = await asyncio.to_thread(self.services.upd_in, msg)
        if res == "bad":
            log.info("канал линка: команда обновления не разобрана — игнорирую")
            return
        if res == "same":
            ack = await asyncio.to_thread(self.services.upd_current_ack)
            await self._send("upd_ack", ack, pad=gwlink.PAD_DELTA)
            return
        if res == "busy":
            await self.flush_upd_ack()
            return
        log.info("набор: команда сервера — обновиться до %s", msg.get("target"))
        self._upd_start()

    async def _upd_run(self, *, revert: bool = False) -> None:
        svc = self.services
        try:
            waited = 0
            while True:
                busy = await asyncio.to_thread(svc.upd_busy)
                if not busy:
                    break
                if waited >= svc_busy_wait():
                    log.warning("набор: команда %s не принята — занят: %s", "отката" if revert else "обновления", busy)
                    await asyncio.to_thread(svc._upd_state, "revert_failed" if revert else "refused",
                                            error=f"занят: {busy}")
                    await self.flush_upd_ack()
                    return
                await asyncio.sleep(BUSY_POLL_S)
                waited += BUSY_POLL_S
            plan = svc.upd_revert_plan if revert else svc.upd_plan
            step, outcome, reason = await asyncio.to_thread(plan)
            if outcome == "done":
                log.info("набор: %s — уже на цели, делать нечего", "откат" if revert else "обновление")
                await asyncio.to_thread(svc._upd_state, "reverted" if revert else "done")
                await self.flush_upd_ack()
                return
            if outcome != "step":
                log.warning("набор: %s не выполнено (%s): %s", "откат" if revert else "обновление", outcome, reason)
                await asyncio.to_thread(svc._upd_state, "revert_failed" if revert else outcome, error=reason)
                await self.flush_upd_ack()
                if not revert:
                    await self._upd_notify("failed")
                return
            if not revert:
                await asyncio.to_thread(svc.upd_accept, step)
                await self.flush_upd_ack()
            await asyncio.sleep(pkg.random.uniform(*PAUSE_S))
            log.info("набор: ступень %s — скачиваю и ставлю", getattr(step, "tag", step))
            try:
                await asyncio.to_thread(svc.upd_apply, step)
                log.info("набор: ступень %s запущена — апдейтер перезапустит агент", getattr(step, "tag", step))
            except Exception as e:                        # noqa: BLE001 — сеть, sha256, место
                log.warning("набор: ступень %s не запустилась: %s", getattr(step, "tag", step), e)
                await asyncio.to_thread(svc._upd_state, "revert_failed" if revert else "failed", error=str(e))
                await self.flush_upd_ack()
                if not revert:
                    await self._upd_notify("failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001
            log.warning("набор: команда обновления не доведена: %s", e)

    async def _upd_resume(self) -> None:
        """Новый процесс, первое слово сервера: довести команду."""
        svc = self.services
        cmd = await asyncio.to_thread(svc.upd_cmd)
        if cmd.get("state") == "applying":
            waited = 0
            while not await asyncio.to_thread(svc.upd_settled) and waited < SETTLE_MAX_S:
                await asyncio.sleep(SETTLE_POLL_S)        # ждём факта: сторож снят — удержание на месте
                waited += SETTLE_POLL_S
        res = await asyncio.to_thread(svc.upd_resume)
        if res in ("done", "rolled_back", "failed", "reverted", "revert_failed"):
            await self.flush_upd_ack()
            if res in ("rolled_back", "failed"):
                await self._upd_notify("failed")
            elif res == "reverted":
                await self._upd_notify("reverted")
        elif res == "next":
            self._upd_start()
        elif res == "check":
            self._upd_start(revert=cmd.get("state") == "reverting")

    async def _upd_commit(self, msg: dict) -> None:
        await asyncio.to_thread(self.services.upd_commit, msg)

    async def _upd_revert(self, msg: dict) -> None:
        res = await asyncio.to_thread(self.services.upd_revert, msg)
        if res == "same":
            ack = await asyncio.to_thread(self.services.upd_current_ack)
            await self._send("upd_ack", ack, pad=gwlink.PAD_DELTA)
        elif res == "done":
            await self.flush_upd_ack()
        elif res == "remote":
            self._upd_start(revert=True)


def svc_busy_wait() -> int:
    from awgbot.domain.gateway.upd import BUSY_WAIT_S
    return BUSY_WAIT_S
