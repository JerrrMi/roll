"""全局单标的交易锁与交易所状态对账。

交易锁必须以交易所持仓与未完成订单校验为最终依据（进程重启后以 Testnet REST 快照恢复）。
任意时刻至多一个合约 symbol 可处于持仓或挂单（ENTRY/EXIT 途中）占用状态。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping

SequenceRows = list[dict[str, Any]]


class TradeLockState(str, Enum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"
    IN_POSITION = "IN_POSITION"
    EXITING = "EXITING"
    COOLDOWN = "COOLDOWN"


class TransitionError(RuntimeError):
    """非法交易锁状态迁移。"""


class MultiSymbolOperationalError(RuntimeError):
    """多标的同时持仓或跨标的多组未完成订单——必须人工处理后方能恢复自动交易。"""


def _decimal_position_amt(raw: Any) -> Decimal | None:
    if isinstance(raw, str):
        try:
            return Decimal(raw)
        except InvalidOperation:
            return None
    return None


def symbol_set_nonzero_positions(position_risk_rows: SequenceRows) -> frozenset[str]:
    """从 GET /positionRisk 行集中提取持仓名义非零的 symbol（一律大写）。 hedge 下同 symbol 多条腿若任一条非零即计入。"""
    out: set[str] = set()
    for row in position_risk_rows:
        if not isinstance(row, Mapping):
            continue
        sym = row.get("symbol")
        amt = _decimal_position_amt(row.get("positionAmt"))
        if not isinstance(sym, str) or amt is None or amt == 0:
            continue
        out.add(sym.strip().upper())
    return frozenset(out)


def symbol_set_open_orders(open_order_rows: SequenceRows) -> frozenset[str]:
    """未完成订单涉及的 symbol（openOrders API 通常为仍活跃挂单）。"""
    out: set[str] = set()
    for row in open_order_rows:
        if not isinstance(row, Mapping):
            continue
        sym = row.get("symbol")
        if isinstance(sym, str) and sym.strip():
            out.add(sym.strip().upper())
    return frozenset(out)


def _parse_reduce_only_flag(row: Mapping[str, Any]) -> bool | None:
    raw = row.get("reduceOnly")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        x = raw.strip().lower()
        if x == "true":
            return True
        if x == "false":
            return False
    return None


def classify_order_only_phase(
    rows_for_single_symbol: list[dict[str, Any]],
) -> tuple[TradeLockState, str | None]:
    """无持仓但存在挂单时推断 ENTERING vs EXITING；无法唯一推断则抛出 MultiSymbolOperationalError。"""
    if not rows_for_single_symbol:
        return TradeLockState.IDLE, None
    inferred: list[bool | None] = []
    syms: set[str] = set()
    for row in rows_for_single_symbol:
        if isinstance(row.get("symbol"), str):
            syms.add(str(row["symbol"]).strip().upper())
        inferred.append(_parse_reduce_only_flag(row))
    if len(syms) != 1:
        raise MultiSymbolOperationalError("openOrders 行集合必须属于单一 symbol（无法归类）")
    sym = next(iter(syms))
    if any(x is None for x in inferred):
        raise MultiSymbolOperationalError(
            "存在无法解析 reduceOnly 的挂单，拒绝自动归类（请人工检查 Testnet openOrders）。"
        )
    if all(inferred):
        return TradeLockState.EXITING, sym
    if not any(inferred):
        return TradeLockState.ENTERING, sym
    raise MultiSymbolOperationalError(
        "同一 symbol 上存在混合 reduceOnly=true/false 的未完成订单；请平仓或撤单后重试。"
    )


@dataclass(frozen=True)
class ReconcileOutcome:
    """以交易所快照为准推导出的运行时姿态。"""

    lock_state: TradeLockState
    active_symbol: str | None
    halt_automatic_trading: bool
    halt_reason: str | None
    position_symbols: frozenset[str]
    order_symbols: frozenset[str]

    def allow_scanning_candidates(self) -> bool:
        return (
            not self.halt_automatic_trading
            and self.lock_state is TradeLockState.IDLE
            and self.active_symbol is None
        )


def reconcile_coin_m_account(
    position_risk_rows: SequenceRows,
    open_order_rows: SequenceRows,
) -> ReconcileOutcome:
    """根据持仓 + 未完成订单推导单标的交易锁状态；违背单标的约束时要求停机待人工处置。"""
    pos_syms = symbol_set_nonzero_positions(position_risk_rows)
    ord_syms = symbol_set_open_orders(open_order_rows)

    if len(pos_syms) > 1:
        return ReconcileOutcome(
            lock_state=TradeLockState.IN_POSITION,
            active_symbol=sorted(pos_syms)[0],
            halt_automatic_trading=True,
            halt_reason=(
                f"检测到 {len(pos_syms)} 个合约同时存在非零持仓 {sorted(pos_syms)}。"
                "请人工减仓或平仓至只剩一个标的后再启动自动交易。"
            ),
            position_symbols=pos_syms,
            order_symbols=ord_syms,
        )

    if len(ord_syms) > 1:
        return ReconcileOutcome(
            lock_state=TradeLockState.ENTERING,
            active_symbol=sorted(ord_syms)[0],
            halt_automatic_trading=True,
            halt_reason=(
                f"检测到多个合约存在未完成挂单 {sorted(ord_syms)}。"
                "请撤单直至只剩单个标的或未成交挂单后再启动。"
            ),
            position_symbols=pos_syms,
            order_symbols=ord_syms,
        )

    if len(pos_syms) == 1:
        (only_pos,) = pos_syms
        stray_orders = ord_syms - frozenset({only_pos})
        if stray_orders:
            return ReconcileOutcome(
                lock_state=TradeLockState.IN_POSITION,
                active_symbol=only_pos,
                halt_automatic_trading=True,
                halt_reason=(
                    f"已持仓合约 {only_pos}，但同时存在其它合约挂单 {sorted(stray_orders)}。"
                    "请撤掉无关挂单或处理后重启。"
                ),
                position_symbols=pos_syms,
                order_symbols=ord_syms,
            )
        orders_same = [dict(r) for r in open_order_rows if str(r.get("symbol", "")).upper() == only_pos]
        if not orders_same:
            return ReconcileOutcome(
                lock_state=TradeLockState.IN_POSITION,
                active_symbol=only_pos,
                halt_automatic_trading=False,
                halt_reason=None,
                position_symbols=pos_syms,
                order_symbols=ord_syms,
            )
        try:
            phase, psym = classify_order_only_phase(orders_same)
        except MultiSymbolOperationalError as e:
            return ReconcileOutcome(
                lock_state=TradeLockState.IN_POSITION,
                active_symbol=only_pos,
                halt_automatic_trading=True,
                halt_reason=str(e),
                position_symbols=pos_syms,
                order_symbols=ord_syms,
            )
        if phase is TradeLockState.EXITING:
            merged = TradeLockState.EXITING
        elif phase is TradeLockState.ENTERING:
            merged = TradeLockState.ENTERING
        else:
            merged = TradeLockState.IN_POSITION
        return ReconcileOutcome(
            lock_state=merged,
            active_symbol=psym or only_pos,
            halt_automatic_trading=False,
            halt_reason=None,
            position_symbols=pos_syms,
            order_symbols=ord_syms,
        )

    if len(ord_syms) == 1:
        (only_ord,) = ord_syms
        rows_ord = [dict(r) for r in open_order_rows if str(r.get("symbol", "")).upper() == only_ord]
        try:
            phase, sym = classify_order_only_phase(rows_ord)
        except MultiSymbolOperationalError as e:
            return ReconcileOutcome(
                lock_state=TradeLockState.ENTERING,
                active_symbol=only_ord,
                halt_automatic_trading=True,
                halt_reason=str(e),
                position_symbols=pos_syms,
                order_symbols=ord_syms,
            )
        return ReconcileOutcome(
            lock_state=phase,
            active_symbol=sym or only_ord,
            halt_automatic_trading=False,
            halt_reason=None,
            position_symbols=pos_syms,
            order_symbols=ord_syms,
        )

    return ReconcileOutcome(
        lock_state=TradeLockState.IDLE,
        active_symbol=None,
        halt_automatic_trading=False,
        halt_reason=None,
        position_symbols=frozenset(),
        order_symbols=frozenset(),
    )


# ---- 迁移图（运行时主动操作；对账可走 restore_from_exchange 捷径） ----
_ALLOWED_EDGES: frozenset[tuple[TradeLockState, TradeLockState]] = frozenset(
    {
        (TradeLockState.IDLE, TradeLockState.ENTERING),
        (TradeLockState.ENTERING, TradeLockState.ENTERING),
        (TradeLockState.ENTERING, TradeLockState.IN_POSITION),
        (TradeLockState.ENTERING, TradeLockState.IDLE),
        (TradeLockState.IN_POSITION, TradeLockState.EXITING),
        (TradeLockState.EXITING, TradeLockState.COOLDOWN),
        (TradeLockState.EXITING, TradeLockState.IN_POSITION),
        (TradeLockState.COOLDOWN, TradeLockState.IDLE),
    }
)


class PositionManager:
    """进程内全局单标的交易锁；应与 `reconcile_coin_m_account` / signed client 快照联合使用。

    exchange 快照通过 `restore_from_exchange` 强制灌入（绕过普通迁移校验）。
    """

    __slots__ = ("_halt_automatic_trading", "_halt_reason", "_rl", "_state", "_sym")

    def __init__(
        self,
        *,
        initial_state: TradeLockState | None = None,
        active_symbol: str | None = None,
        halted: bool = False,
        halt_reason: str | None = None,
    ) -> None:
        self._rl = threading.RLock()
        self._state = TradeLockState.IDLE if initial_state is None else initial_state
        self._sym: str | None = active_symbol
        self._halt_automatic_trading = halted
        self._halt_reason = halt_reason

    @property
    def lock_state(self) -> TradeLockState:
        with self._rl:
            return self._state

    @property
    def active_symbol(self) -> str | None:
        with self._rl:
            return self._sym

    @property
    def halt_automatic_trading(self) -> bool:
        with self._rl:
            return self._halt_automatic_trading

    @property
    def halt_reason(self) -> str | None:
        with self._rl:
            return self._halt_reason

    def set_halt_for_manual_review(self, reason: str) -> None:
        with self._rl:
            self._halt_automatic_trading = True
            self._halt_reason = reason

    def clear_halt_if_safe(self, *, outcome: ReconcileOutcome | None = None) -> None:
        with self._rl:
            if outcome is not None and outcome.halt_automatic_trading:
                return
            self._halt_automatic_trading = False
            self._halt_reason = None

    def restore_from_exchange(self, outcome: ReconcileOutcome) -> None:
        """以 Testnet REST 推导结果为准覆盖本地视图。"""
        with self._rl:
            self._state = outcome.lock_state
            self._sym = outcome.active_symbol
            self._halt_automatic_trading = outcome.halt_automatic_trading
            self._halt_reason = outcome.halt_reason

    def assert_single_focus_or_raise(self, symbol: str, *, intent: str) -> None:
        """准备对 `symbol` 下单前调用：若与其它标的占位冲突则报错。"""
        sym = symbol.strip().upper()
        with self._rl:
            if self._halt_automatic_trading:
                raise TransitionError(f"自动交易已挂起 ({intent})：{self._halt_reason or '参阅 halt_reason'}")
            active = self._sym
            st = self._state
        if active is not None and active != sym:
            raise TransitionError(f"{intent} 拒绝：会话已占用标的 {active}，请求 {sym}")
        if st in {TradeLockState.EXITING, TradeLockState.COOLDOWN} and sym != active:
            raise TransitionError(f"{intent} 拒绝：状态={st.value}，已绑定 {active}。")

    def begin_enter(self, symbol: str) -> None:
        """IDLE → ENTERING(symbol)"""
        sym = symbol.strip().upper()
        self._transition(TradeLockState.ENTERING, sym, validator=self._idle_or_same_entering(sym))

    def rollback_enter_to_idle(self) -> None:
        """入场失败撤销：ENTERING → IDLE。"""
        self._transition(TradeLockState.IDLE, None, validator=self._expects(TradeLockState.ENTERING))

    def confirm_in_position(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        self._transition(
            TradeLockState.IN_POSITION,
            sym,
            validator=self._entering_matching(sym),
        )

    def begin_exit(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        self._transition(
            TradeLockState.EXITING,
            sym,
            validator=self._in_position_matching(sym),
        )

    def mark_exit_finished_to_cooldown(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        self._transition(
            TradeLockState.COOLDOWN,
            sym,
            validator=self._exiting_matching(sym),
        )

    def mark_exit_abort_in_position(self, symbol: str) -> None:
        """EXITING → IN_POSITION（仅部分平仓或未成交而退）。"""
        sym = symbol.strip().upper()
        self._transition(
            TradeLockState.IN_POSITION,
            sym,
            validator=self._exiting_matching(sym),
        )

    def finish_cooldown_to_idle(self) -> None:
        self._transition(TradeLockState.IDLE, None, validator=self._expects(TradeLockState.COOLDOWN))

    def allow_scan_candidates(self) -> bool:
        with self._rl:
            return (
                not self._halt_automatic_trading
                and self._state is TradeLockState.IDLE
                and self._sym is None
            )

    def allow_manage_active_only(self, symbol: str) -> bool:
        sym = symbol.strip().upper()
        with self._rl:
            if self._halt_automatic_trading:
                return False
            if self._sym != sym:
                return False
            return self._state in {
                TradeLockState.ENTERING,
                TradeLockState.IN_POSITION,
                TradeLockState.EXITING,
                TradeLockState.COOLDOWN,
            }

    def _idle_or_same_entering(self, sym: str) -> Callable[[TradeLockState, str | None], None]:
        def _chk(prev_state: TradeLockState, prev_sym: str | None) -> None:
            if prev_state is TradeLockState.IDLE:
                return
            if prev_state is TradeLockState.ENTERING and prev_sym == sym:
                return
            raise TransitionError(f"无法从 {prev_state.value}/{prev_sym} 进入 ENTERING({sym})")

        return _chk

    def _entering_matching(self, sym: str) -> Callable[[TradeLockState, str | None], None]:
        def _chk(prev_state: TradeLockState, prev_sym: str | None) -> None:
            if prev_state is TradeLockState.ENTERING and prev_sym == sym:
                return
            raise TransitionError(f"确认持仓需要 ENTERING({sym})，实际 {prev_state.value}/{prev_sym}")

        return _chk

    def _in_position_matching(self, sym: str) -> Callable[[TradeLockState, str | None], None]:
        def _chk(prev_state: TradeLockState, prev_sym: str | None) -> None:
            if prev_state is TradeLockState.IN_POSITION and prev_sym == sym:
                return
            raise TransitionError(f"开始平仓需要从 IN_POSITION({sym})，实际 {prev_state.value}/{prev_sym}")

        return _chk

    def _exiting_matching(self, sym: str) -> Callable[[TradeLockState, str | None], None]:
        def _chk(prev_state: TradeLockState, prev_sym: str | None) -> None:
            if prev_state is TradeLockState.EXITING and prev_sym == sym:
                return
            raise TransitionError(f"期望 EXITING({sym})，实际 {prev_state.value}/{prev_sym}")

        return _chk

    def _expects(
        self, want: TradeLockState
    ) -> Callable[[TradeLockState, str | None], None]:
        def _chk(prev_state: TradeLockState, _prev_sym: str | None) -> None:
            if prev_state != want:
                raise TransitionError(f"期望前置状态 {want.value}，实际 {prev_state.value}")

        return _chk

    def _transition(
        self,
        new_state: TradeLockState,
        new_symbol: str | None,
        *,
        validator: Callable[[TradeLockState, str | None], None] | None = None,
        force: bool = False,
    ) -> None:
        with self._rl:
            if self._halt_automatic_trading and not force:
                raise TransitionError(f"自动交易已挂起：{self._halt_reason or '参阅 halt_reason'}")

            prev_state = self._state
            prev_sym = self._sym
            if validator is not None:
                validator(prev_state, prev_sym)

            if not force:
                edge = (prev_state, new_state)
                if edge not in _ALLOWED_EDGES:
                    allowed = sorted({to.value for (fr, to) in _ALLOWED_EDGES if fr == prev_state})
                    raise TransitionError(
                        f"非法迁移 {prev_state.value}→{new_state.value}。"
                        + (f" 允许后继：{allowed}" if allowed else " 无前驱定义。")
                    )

            if new_state is TradeLockState.IDLE:
                next_sym: str | None = None
            elif new_state is TradeLockState.ENTERING:
                if new_symbol is None:
                    raise TransitionError("迁移到 ENTERING 必须传入 symbol")
                next_sym = new_symbol.strip().upper()
            else:
                if new_symbol is not None:
                    next_sym = new_symbol.strip().upper()
                else:
                    next_sym = prev_sym
                if next_sym is None:
                    raise TransitionError(f"迁移到 {new_state.value} 时缺少活跃 symbol")

            if prev_sym is not None and next_sym is not None and prev_sym != next_sym:
                raise TransitionError(
                    f"禁止在单笔会话中切换合约：前置 {prev_sym}，后继 {next_sym}"
                )

            self._sym = next_sym
            self._state = new_state

    def snapshot_dict(self) -> dict[str, Any]:
        with self._rl:
            return {
                "trade_lock_state": self._state.value,
                "active_symbol": self._sym,
                "halt_automatic_trading": self._halt_automatic_trading,
                "halt_reason": self._halt_reason,
            }


def bootstrap_position_manager_from_exchange_client(
    client: Any,
) -> tuple[PositionManager, ReconcileOutcome, tuple[int, int, int]]:
    """返回 ``(manager, reconcile_outcome, (offset_ms, n_position_rows, n_open_orders))``。"""
    offset_ms = int(client.sync_server_time())
    poses = client.position_risk()
    orders = client.open_orders()
    outcome = reconcile_coin_m_account(poses, orders)
    mgr = PositionManager()
    mgr.restore_from_exchange(outcome)
    return mgr, outcome, (offset_ms, len(poses), len(orders))


def position_manager_from_saved_view(
    *,
    trade_lock_state: str,
    active_symbol: str | None,
    halt_automatic_trading: bool,
    halt_reason: str | None,
) -> PositionManager:
    """从磁盘 `RuntimeState` 字段恢复会话锁（仍以启动对账结果为最高优先级）。"""
    try:
        st = TradeLockState(trade_lock_state)
    except ValueError:
        st = TradeLockState.IDLE
    sym_clean = active_symbol.strip().upper() if isinstance(active_symbol, str) else None
    sym_out = sym_clean if sym_clean else None
    return PositionManager(
        initial_state=st,
        active_symbol=sym_out,
        halted=halt_automatic_trading,
        halt_reason=halt_reason,
    )


def reconcile_from_signed_client(position_risk_rows: SequenceRows, open_orders_rows: SequenceRows) -> ReconcileOutcome:
    """对账适配器别名，便于交易策略代码统一命名。"""
    return reconcile_coin_m_account(position_risk_rows, open_orders_rows)
