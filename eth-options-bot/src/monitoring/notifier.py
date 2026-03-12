from __future__ import annotations

import logging
from typing import Optional
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send alerts via Telegram bot."""

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{token}"

    def send(self, message: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        try:
            resp = requests.post(
                f"{self._base}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=10,
            )
            return resp.ok
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False


class SlackNotifier:
    """Send alerts via Slack incoming webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, message: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            resp = requests.post(
                self.webhook_url,
                json={"text": message},
                timeout=10,
            )
            return resp.ok
        except Exception as exc:
            logger.error("Slack send failed: %s", exc)
            return False


class WhatsAppNotifier:
    """
    Send WhatsApp messages via CallMeBot (free, no business account needed).

    Setup (one-time, takes ~1 minute):
      1. Add +34 644 59 79 22 to your WhatsApp contacts (name: CallMeBot)
      2. Send this message to that contact via WhatsApp:
           I allow callmebot to send me messages
      3. You will receive your API key by WhatsApp within 60 seconds.
      4. Set phone (international format, no + or spaces, e.g. 447911123456)
         and the received apikey in config.yaml or env vars.
    """

    _URL = "https://api.callmebot.com/whatsapp.php"

    def __init__(self, phone: str, apikey: str) -> None:
        self.phone  = phone.strip().lstrip("+").replace(" ", "")
        self.apikey = apikey.strip()

    def send(self, message: str) -> bool:
        if not self.phone or not self.apikey:
            return False
        try:
            resp = requests.get(
                self._URL,
                params={"phone": self.phone, "text": message, "apikey": self.apikey},
                timeout=15,
            )
            if not resp.ok:
                logger.error("WhatsApp send failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return resp.ok
        except Exception as exc:
            logger.error("WhatsApp send failed: %s", exc)
            return False


class AlertManager:
    """
    Unified alert manager that dispatches to configured notifiers.
    Falls back to log-only if no notifiers are configured.
    """

    def __init__(
        self,
        telegram: Optional[TelegramNotifier] = None,
        slack: Optional[SlackNotifier] = None,
        whatsapp: Optional[WhatsAppNotifier] = None,
    ) -> None:
        self._notifiers = []
        if telegram:
            self._notifiers.append(telegram)
        if slack:
            self._notifiers.append(slack)
        if whatsapp:
            self._notifiers.append(whatsapp)

    def alert(self, message: str, level: str = "INFO") -> None:
        prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]", "TRADE": "[TRADE]"}.get(level, "")
        full = f"{prefix} ETH Options Bot\n{message}"
        logger.log(getattr(logging, level.replace("TRADE", "INFO"), logging.INFO), message)
        for notifier in self._notifiers:
            notifier.send(full)

    def trade_opened(self, condor, spot: float = 0.0) -> None:
        stype = condor.__dict__.get("spread_type", "spread").replace("_", " ").upper()
        lines = [f"TRADE OPENED -- {stype}", f"ID: {condor.id}"]

        sc = condor.short_call
        lc = condor.long_call
        sp = condor.short_put
        lp = condor.long_put

        if sc.instrument_name != "STUB":
            lines.append(f"SELL {sc.instrument_name}  d={sc.delta:+.2f}")
            lines.append(f"BUY  {lc.instrument_name}  d={lc.delta:+.2f}")
        if sp.instrument_name != "STUB":
            lines.append(f"SELL {sp.instrument_name}  d={sp.delta:+.2f}")
            lines.append(f"BUY  {lp.instrument_name}  d={lp.delta:+.2f}")

        lines.append(f"Credit : {condor.credit_received:.5f} ETH")
        lines.append(f"Max loss: {condor.max_loss:.5f} ETH")
        if spot:
            lines.append(f"ETH spot: ${spot:,.0f}")
            lines.append(f"TP at: +{condor.credit_received * 0.5:.5f} ETH (+${condor.credit_received * 0.5 * spot:+.2f})")
            lines.append(f"SL at: -{condor.credit_received * 1.5:.5f} ETH (-${condor.credit_received * 1.5 * spot:.2f})")

        self.alert("\n".join(lines), level="TRADE")

    def trade_closed(self, condor, spot: float = 0.0) -> None:
        pnl = condor.realized_pnl or 0.0
        pnl_usd = pnl * spot if spot else 0.0
        result = "PROFIT" if pnl > 0 else "LOSS"
        stype = condor.__dict__.get("spread_type", "spread").replace("_", " ").upper()
        lines = [
            f"TRADE CLOSED -- {stype} -- {result}",
            f"ID: {condor.id}",
            f"PnL: {pnl:+.5f} ETH" + (f"  (~${pnl_usd:+.2f})" if spot else ""),
            f"Reason: {condor.exit_reason or 'unknown'}",
        ]
        self.alert("\n".join(lines), level="TRADE")

    def risk_alert(self, message: str) -> None:
        self.alert(f"RISK ALERT: {message}", level="ERROR")

    def daily_summary(self, date: str, daily_pnl: float, equity: float) -> None:
        msg = (
            f"Daily Summary | {date}\n"
            f"Daily PnL: {daily_pnl:.4f} ETH\n"
            f"Equity: {equity:.2f} USD"
        )
        self.alert(msg, level="INFO")
