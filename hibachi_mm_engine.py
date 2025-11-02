from __future__ import annotations
import logging, csv, os, random, time
from typing import Optional
from dataclasses import dataclass, field

from utils import ATR, bps_to_price, pct_of, clamp, ContractSpec, get_precision
from hibachi_client import HibachiRest

log = logging.getLogger("hibachi.mm")


def _to_float(x) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except:
        return None


@dataclass
class Bar:
    o: float = 0
    h: float = 0
    l: float = 0
    c: float = 0
    closed: bool = False


@dataclass
class SideState:
    client_id: Optional[str] = None
    order_id: Optional[str] = None
    price: Optional[float] = None
    qty: Optional[float] = None


@dataclass
class MMState:
    bid: SideState = field(default_factory=SideState)
    ask: SideState = field(default_factory=SideState)
    prev_mid: Optional[float] = None
    equity_usd: float = 0.0
    pos_qty: float = 0.0
    mark_price: float = 0.0
    last_bar: Bar = field(default_factory=Bar)
    last_equity_update: float = 0.0
    order_count_1min: int = 0
    last_order_reset: float = 0.0
    quote_count: int = 0


class HibachiMarketMakerEngine:
    def __init__(self, rest: HibachiRest, cfg: dict, logs_dir: str):
        self.rest = rest
        self.cfg = cfg
        self.symbol = cfg["symbol"]
        self.params = cfg
        self.state = MMState()
        self.contract: Optional[ContractSpec] = None
        self.atr = ATR(int(cfg["atrLen"]))
        self.trades_log_path = os.path.join(logs_dir, "trades.csv")
        self._ensure_trade_log_header()
        self.max_orders_per_min = 30

        self.target_leverage = int(cfg.get("leverage", 1))
        if self.target_leverage != 1:
            log.warning("⚠️ LEVERAGE=%d in config, but strategy designed for 1x!",
                        self.target_leverage)
            log.warning("⚠️ Forcing leverage=1 for safety")
            self.target_leverage = 1

    def _ensure_trade_log_header(self):
        if not os.path.exists(self.trades_log_path):
            os.makedirs(os.path.dirname(self.trades_log_path), exist_ok=True)
            with open(self.trades_log_path, "w", newline="") as f:
                csv.writer(f).writerow([
                    "ts", "symbol", "side", "price", "qty", "fee", "orderId", "realizedPnl"
                ])

    def _check_rate_limit(self) -> bool:
        now = time.time()
        if now - self.state.last_order_reset >= 60:
            self.state.order_count_1min = 0
            self.state.last_order_reset = now
        if self.state.order_count_1min >= self.max_orders_per_min:
            log.warning("Rate limit: %d orders/min", self.state.order_count_1min)
            return False
        return True

    def _increment_order_count(self):
        self.state.order_count_1min += 1

    def bootstrap_markets(self):
        log.info("Loading contract info for %s...", self.symbol)
        contract_info = self.rest.get_contract_info(self.symbol)
        if not contract_info:
            raise RuntimeError(f"Contract {self.symbol} not found")

        tick_size = contract_info.get('tickSize') or contract_info.get('tick_size') or 0.01
        step_size = contract_info.get('stepSize') or contract_info.get('step_size') or 0.001
        min_qty = contract_info.get('minOrderSize') or contract_info.get('min_order_size') or 0.001
        min_notional = contract_info.get('minNotional') or contract_info.get('min_notional') or 10.0
        contract_size = contract_info.get('contractSize') or contract_info.get('contract_size') or 1.0

        self.contract = ContractSpec(
            symbol=self.symbol,
            tick_size=float(tick_size),
            step_size=float(step_size),
            min_qty=float(min_qty),
            min_notional=float(min_notional),
            contract_size=float(contract_size)
        )
        log.info("Contract: tick=%.6f step=%.6f size=%.6f min_notional=%.2f",
                 self.contract.tick_size, self.contract.step_size,
                 self.contract.contract_size, self.contract.min_notional)

        log.info("=" * 70)
        log.info("Setting leverage to %dx for %s...", self.target_leverage, self.symbol)
        log.info("=" * 70)

        try:
            result = self.rest.set_leverage(self.symbol, self.target_leverage)

            if result.get('status') == 'not_supported':
                log.error("⚠️" * 35)
                log.error("⚠️ CRITICAL: SDK does not support set_leverage()")
                log.error("⚠️ You MUST manually set leverage to %dx in Hibachi:", self.target_leverage)
                log.error("⚠️ 1. Go to Hibachi web interface")
                log.error("⚠️ 2. Navigate to Futures → Position Settings")
                log.error("⚠️ 3. Set leverage to %dx for %s", self.target_leverage, self.symbol)
                log.error("⚠️ 4. Restart the bot")
                log.error("⚠️" * 35)

                log.error("Starting in 10 seconds... (Ctrl+C to abort)")
                for i in range(10, 0, -1):
                    log.error("%d...", i)
                    time.sleep(1)
            else:
                log.info("✓ Leverage successfully set to %dx for %s",
                         self.target_leverage, self.symbol)

        except Exception as e:
            log.error("⚠️" * 35)
            log.error("⚠️ CRITICAL: Failed to set leverage: %s", e)
            log.error("⚠️ You MUST manually set leverage to %dx!", self.target_leverage)
            log.error("⚠️ Go to Hibachi web interface and set it manually")
            log.error("⚠️" * 35)
            raise RuntimeError("Cannot proceed without setting leverage to 1x")

        log.info("Canceling all existing orders...")
        try:
            result = self.rest.cancel_all_orders()
            log.info("✓ All existing orders canceled: %s", result)
        except Exception as e:
            log.warning("Could not cancel existing orders: %s", e)
            log.info("Continuing anyway...")

    def bootstrap_atr(self):
        try:
            timeframe = self.params.get("atrTimeframe", "5m")
            atr_len = int(self.params["atrLen"])
            limit = min(atr_len + 10, 100)

            klines = self.rest.get_klines(self.symbol, interval=timeframe, limit=limit)

            if not klines:
                log.warning("Could not fetch klines for ATR initialization")
                return

            count = 0
            for candle in klines:
                if isinstance(candle, (list, tuple)) and len(candle) >= 5:
                    o, h, l, c = float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])
                    self.atr.update_bar(o, h, l, c, closed=True)
                    count += 1

            if self.atr.rma:
                log.info("ATR initialized from %d %s candles: %.2f", count, timeframe, self.atr.rma)
            else:
                log.warning("ATR initialization incomplete")

        except Exception as e:
            log.warning("Failed to bootstrap ATR from klines: %s", e)
            log.info("ATR will be initialized from live ticks")

    def bootstrap_equity_and_pos(self):
        try:
            account = self.rest.get_account_info()
            self.state.equity_usd = float(account.get('balance', 0))

            position = self.rest.get_position(self.symbol)
            if position:
                self.state.pos_qty = float(position.get('size', 0))
                mark_price = position.get('markPrice') or position.get('mark_price')
                if mark_price:
                    self.state.mark_price = float(mark_price)
            else:
                self.state.pos_qty = 0.0
                mid = self.compute_mid()
                if mid:
                    self.state.mark_price = mid

            if self.state.pos_qty != 0:
                notional = self.state.pos_qty * self.state.mark_price
                log.info("Position: %.6f contracts ($%.2f notional @ $%.2f)",
                         self.state.pos_qty, notional, self.state.mark_price)

            log.info("Balance: $%.2f | Position: %.4f contracts | Leverage: %dx",
                     self.state.equity_usd, self.state.pos_qty, self.target_leverage)
        except Exception as e:
            log.error("Bootstrap failed: %s", e)
            raise

    def _force_equity_update(self) -> float:
        try:
            account = self.rest.get_account_info()
            self.state.equity_usd = float(account.get('balance', 0))
            position = self.rest.get_position(self.symbol)
            if position:
                self.state.pos_qty = float(position.get('size', 0))
                mark_price = position.get('markPrice') or position.get('mark_price')
                if mark_price:
                    self.state.mark_price = float(mark_price)
            self.state.last_equity_update = time.time()
        except Exception as e:
            log.error("Update equity failed: %s", e)
        return self.state.equity_usd

    def compute_equity_usd(self, force: bool = False) -> float:
        if force or (time.time() - self.state.last_equity_update) >= 60:
            return self._force_equity_update()
        return self.state.equity_usd

    def update_bar_from_ticker(self):
        ticker = self.rest.get_ticker(self.symbol)
        if not ticker:
            return

        last_price = _to_float(ticker.get('lastPrice') or ticker.get('last_price') or ticker.get('last'))
        if last_price:
            if self.state.last_bar.c == 0:
                self.state.last_bar = Bar(last_price, last_price, last_price, last_price, True)
            else:
                self.state.last_bar.c = last_price
                self.state.last_bar.h = max(self.state.last_bar.h, last_price)
                self.state.last_bar.l = min(self.state.last_bar.l, last_price)

            mark = _to_float(ticker.get('markPrice') or ticker.get('mark_price'))
            if mark:
                self.state.mark_price = mark

    def compute_mid(self) -> Optional[float]:
        try:
            mid = self.rest.get_mid_price(self.symbol)
            if mid and mid > 0:
                self.state.mark_price = mid
                return mid
            log.warning("Unable to get mid price")
            return None
        except Exception as e:
            log.error("compute_mid error: %s", e)
            return None

    def get_funding_rate(self) -> Optional[float]:
        ticker = self.rest.get_ticker(self.symbol)
        if ticker:
            return _to_float(ticker.get('fundingRate') or ticker.get('funding_rate'))
        return None

    def step(self):
        if not self.contract:
            return

        self.update_bar_from_ticker()
        m = self.compute_mid()
        if not m or m <= 0:
            log.warning("No valid mid price")
            return

        funding = self.get_funding_rate()
        if funding and abs(funding) > 0.01:
            log.warning("High funding rate: %.4f%%", funding * 100)

        b = self.state.last_bar
        if b.c > 0:
            atr_val, tr1 = self.atr.update_bar(b.o, b.h, b.l, b.c, True)
            if atr_val <= 0:
                atr_val = m * 0.001
                tr1 = atr_val
        else:
            atr_val = m * 0.001
            tr1 = 0

        atr_val = max(atr_val, m * 0.0005)

        big_move = (self.params.get("slipGuardATR", 0) > 0 and tr1 > self.params["slipGuardATR"] * atr_val)

        if self.state.prev_mid is None:
            mid_changed = True
        else:
            mid_changed = abs(m - self.state.prev_mid) > bps_to_price(m, self.params.get("requoteBps", 10.0))

        should_quote = (not big_move) and mid_changed

        equity = self.compute_equity_usd()
        position_notional = self.state.pos_qty * m
        max_notional = equity * (self.params["invBudgetPct"] / 100)

        if max_notional <= 0:
            log.warning("Invalid max_notional: %.2f", max_notional)
            return

        skew = clamp(position_notional / max_notional if max_notional > 0 else 0, -1.0, 1.0)

        half_floor = bps_to_price(m, self.params["minFullBps"] * 0.5)
        half_ceil = bps_to_price(m, self.params["maxFullBps"] * 0.5)
        half_w = clamp(self.params["kATR"] * atr_val, half_floor, half_ceil)

        bull_shift = bps_to_price(m, self.params.get("bullBiasBps", 0)) if self.params.get("useBullBias",
                                                                                           False) else 0.0

        sgn = 1.0 if skew > 0 else (-1.0 if skew < 0 else 0.0)
        inv_offset = self.params.get("skewDamp", 0.3) * abs(skew) * half_w * sgn

        ask_px = m + half_w + bull_shift - inv_offset
        bid_px = m - half_w + bull_shift - inv_offset

        # 🔧 FIX: Enforce minimum spread from mid (safety margin)
        min_spread_pct = 0.0015  # 0.15% минимум от mid
        min_offset = m * min_spread_pct

        # Проверяем, что bid ниже mid, а ask выше mid
        if bid_px >= m - min_offset:
            bid_px = m - min_offset
            log.debug("⚠️ Bid too close to mid, adjusted to %.2f", bid_px)

        if ask_px <= m + min_offset:
            ask_px = m + min_offset
            log.debug("⚠️ Ask too close to mid, adjusted to %.2f", ask_px)

        base_usd = pct_of(equity, self.params["baseOrderPct"])
        size_amp = self.params.get("sizeAmp", 1.5)
        ask_mult = 1.0 + size_amp * max(0.0, skew)
        bid_mult = 1.0 + size_amp * max(0.0, -skew)

        bid_notional = base_usd * bid_mult
        ask_notional = base_usd * ask_mult

        if bid_px <= 0 or ask_px <= 0:
            log.error("Invalid prices: bid_px=%.2f ask_px=%.2f", bid_px, ask_px)
            return

        bid_contracts = bid_notional / bid_px
        ask_contracts = ask_notional / ask_px

        bid_qty_q = self.contract.q_qty(bid_contracts)
        ask_qty_q = self.contract.q_qty(ask_contracts)

        ask_px_q = self.contract.q_price_ceil(ask_px)
        bid_px_q = self.contract.q_price_floor(bid_px)

        if ask_px_q <= 0 or bid_px_q <= 0:
            log.error("Invalid quantized prices: bid=%.2f ask=%.2f", bid_px_q, ask_px_q)
            return

        # 🔧 FIX: Final sanity check after quantization
        if bid_px_q >= m:
            log.error("❌ BID ABOVE MID after quantization! bid=%.2f mid=%.2f", bid_px_q, m)
            bid_px_q = self.contract.q_price_floor(m - min_offset)
            log.info("✓ Corrected bid to %.2f", bid_px_q)

        if ask_px_q <= m:
            log.error("❌ ASK BELOW MID after quantization! ask=%.2f mid=%.2f", ask_px_q, m)
            ask_px_q = self.contract.q_price_ceil(m + min_offset)
            log.info("✓ Corrected ask to %.2f", ask_px_q)

        can_buy = position_notional < max_notional
        if self.params.get("longBiasOnly", False):
            can_sell = self.state.pos_qty > 0
        else:
            can_sell = position_notional > -max_notional

        min_notional_check = self.contract.min_notional

        bid_notional_check = bid_px_q * bid_qty_q
        ask_notional_check = ask_px_q * ask_qty_q

        ask_ok = (ask_qty_q >= self.contract.min_qty and ask_notional_check >= min_notional_check)
        bid_ok = (bid_qty_q >= self.contract.min_qty and bid_notional_check >= min_notional_check)

        log.info("mid=%.2f bid=%.2f ask=%.2f skew=%.3f pos=%.4f eq=$%.0f atr=%.2f hw=%.2f (%dx)",
                 m, bid_px_q, ask_px_q, skew, self.state.pos_qty, equity, atr_val, half_w, self.target_leverage)

        if should_quote:
            if not self._check_rate_limit():
                return

            self._cancel_both()
            self.state.quote_count += 1

            if can_buy and bid_ok:
                new_bid = self._place_limit("BUY", bid_px_q, bid_qty_q)
                if new_bid.order_id:
                    self.state.bid = new_bid
                    log.debug("Saved BID order_id: %s", new_bid.order_id)
            else:
                if not can_buy:
                    log.debug("Skip BID: limit (%.2f >= %.2f)", position_notional, max_notional)
                elif not bid_ok:
                    log.debug("Skip BID: notional %.2f < %.2f or qty %.6f < %.6f",
                              bid_notional_check, min_notional_check, bid_qty_q, self.contract.min_qty)

            if can_sell and ask_ok:
                new_ask = self._place_limit("SELL", ask_px_q, ask_qty_q)
                if new_ask.order_id:
                    self.state.ask = new_ask
                    log.debug("Saved ASK order_id: %s", new_ask.order_id)
            else:
                if not can_sell:
                    if self.params.get("longBiasOnly", False):
                        log.debug("Skip ASK: long-only (pos=%.6f)", self.state.pos_qty)
                    else:
                        log.debug("Skip ASK: limit (%.2f <= %.2f)", position_notional, -max_notional)
                elif not ask_ok:
                    log.debug("Skip ASK: notional %.2f < %.2f or qty %.6f < %.6f",
                              ask_notional_check, min_notional_check, ask_qty_q, self.contract.min_qty)

            self.state.prev_mid = m

    def _new_client_id(self) -> str:
        return f"mm_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"

    def _place_limit(self, side: str, price: float, qty: float) -> SideState:
        cid = self._new_client_id()
        price_precision = get_precision(self.contract.tick_size)
        qty_precision = get_precision(self.contract.step_size)
        price_str = f"{price:.{price_precision}f}"
        qty_str = f"{qty:.{qty_precision}f}"

        try:
            res = self.rest.place_order(
                symbol=self.symbol, side=side, order_type="LIMIT",
                price=price_str, quantity=qty_str,
                time_in_force=self.params.get("timeInForce", "GTC"),
                post_only=bool(self.params.get("postOnly", True)),
                reduce_only=False, client_order_id=cid
            )

            oid = None
            if isinstance(res, dict):
                oid = (res.get("orderId") or res.get("order_id") or
                       res.get("id") or res.get("orderID") or res.get("order"))

                if oid:
                    oid = str(oid)
                    log.info("PLACE %s @ %s x %s -> %s", side, price_str, qty_str, oid)
                else:
                    log.error("PLACE %s @ %s x %s -> NO ORDER_ID!", side, price_str, qty_str)
                    log.error("Response keys: %s", list(res.keys()))
                    log.error("Full response: %s", res)
            else:
                log.error("PLACE %s: unexpected response type: %s", side, type(res))
                log.error("Response: %s", res)

            self._increment_order_count()
            return SideState(cid, oid, float(price_str), float(qty_str))

        except Exception as e:
            error_msg = str(e)
            log.error("Place %s FAILED: %s", side, error_msg)
            log.error("  Price: %s | Qty: %s", price_str, qty_str)

            if "RISK" in error_msg.upper() or "LIMIT" in error_msg.upper():
                log.error("⚠️ RISK LIMIT EXCEEDED!")
                log.error("⚠️ Current: BASE_ORDER_PCT=%.1f%%, INV_BUDGET_PCT=%.1f%%",
                          self.params["baseOrderPct"], self.params["invBudgetPct"])
                log.error("⚠️ Try: BASE_ORDER_PCT=0.5, INV_BUDGET_PCT=20.0")

            return SideState()

    def _cancel_side(self, side: str):
        st = self.state.bid if side == "bid" else self.state.ask

        if not st.order_id:
            log.debug("Skip cancel %s: no order_id", side.upper())
            return

        log.debug("Attempting to cancel %s order_id: %s", side.upper(), st.order_id)

        try:
            result = self.rest.cancel_order(symbol=self.symbol, order_id=st.order_id)
            status = result.get('status') if isinstance(result, dict) else None
            if status == 'CANCELED':
                log.info("CANCEL %s SUCCESS (order_id: %s)", side.upper(), st.order_id)
            else:
                log.info("CANCEL %s: status=%s (order_id: %s)", side.upper(), status, st.order_id)
        except Exception as e:
            error_msg = str(e).lower()
            if "not found" in error_msg or "unknown order" in error_msg:
                log.info("CANCEL %s: already filled/cancelled (order_id: %s)", side.upper(), st.order_id)
            else:
                log.error("Cancel %s failed: %s (order_id: %s)", side, e, st.order_id)
        finally:
            if side == "bid":
                self.state.bid = SideState()
            else:
                self.state.ask = SideState()

    def _cancel_both(self):
        log.debug("Canceling both sides...")
        self._cancel_side("bid")
        self._cancel_side("ask")
        log.debug("Both sides canceled")