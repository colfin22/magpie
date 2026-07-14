"""Multi-channel notification fan-out."""
from app import config, ha


class Rec:
    """Records httpx.post calls; every response is a success."""
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def post(self, url, **kw):
        self.calls.append((url, kw))
        fail = self.fail_on and self.fail_on in url

        class R:
            def raise_for_status(self_):
                if fail:
                    raise RuntimeError("boom")
        return R()


def _clear(mp):
    for k in ("HA_URL", "HA_TOKEN", "HA_NOTIFY_SERVICE", "PUSHOVER_TOKEN", "PUSHOVER_USER",
              "PUSHBULLET_TOKEN", "DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "NTFY_TOPIC"):
        mp.setattr(config, k, "")
    mp.setattr(config, "NTFY_SERVER", "https://ntfy.sh")
    mp.setattr(config, "HA_NOTIFY_CLICK_URL", "https://magpie.example")


def test_no_channels_returns_false(monkeypatch):
    _clear(monkeypatch)
    rec = Rec(); monkeypatch.setattr(ha, "httpx", rec)
    assert ha.notify("t", "m") is False
    assert rec.calls == []


def test_pushover_payload(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "PUSHOVER_TOKEN", "tok")
    monkeypatch.setattr(config, "PUSHOVER_USER", "usr")
    rec = Rec(); monkeypatch.setattr(ha, "httpx", rec)
    assert ha.notify("Title", "Body") is True
    url, kw = rec.calls[0]
    assert "api.pushover.net" in url
    assert kw["data"] == {"token": "tok", "user": "usr", "title": "Title",
                          "message": "Body", "url": "https://magpie.example"}


def test_discord_embed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.test/wh")
    rec = Rec(); monkeypatch.setattr(ha, "httpx", rec)
    ha.notify("Title", "Body")
    url, kw = rec.calls[0]
    assert url == "https://discord.test/wh"
    e = kw["json"]["embeds"][0]
    assert e["title"] == "Title" and e["description"] == "Body" and e["url"] == "https://magpie.example"


def test_fans_out_to_all_configured(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "PUSHOVER_TOKEN", "t"); monkeypatch.setattr(config, "PUSHOVER_USER", "u")
    monkeypatch.setattr(config, "PUSHBULLET_TOKEN", "pb")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://d.test/wh")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "bt"); monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(config, "NTFY_TOPIC", "topic")
    rec = Rec(); monkeypatch.setattr(ha, "httpx", rec)
    assert ha.notify("t", "m") is True
    urls = " ".join(u for u, _ in rec.calls)
    assert "pushover" in urls and "pushbullet" in urls and "d.test/wh" in urls
    assert "telegram.org/botbt/sendMessage" in urls and "ntfy.sh/topic" in urls
    assert len(rec.calls) == 5


def test_one_channel_failure_does_not_block_others(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "PUSHOVER_TOKEN", "t"); monkeypatch.setattr(config, "PUSHOVER_USER", "u")
    monkeypatch.setattr(config, "NTFY_TOPIC", "topic")
    rec = Rec(fail_on="pushover"); monkeypatch.setattr(ha, "httpx", rec)
    assert ha.notify("t", "m") is True                     # ntfy still delivered
    assert any("ntfy.sh/topic" in u for u, _ in rec.calls)


# --- #75 / PR #76: model prose must never make a message unsendable -----------

def test_telegram_escapes_prose_that_would_otherwise_400(monkeypatch):
    """Telegram rejects the WHOLE message with a 400 on markup its parser cannot read.
    Markdown died on an unbalanced _ or *; HTML dies just as readily on a bare < or & --
    and trading prose is full of both ("RSI < 30 and risk & reward favour the entry").
    Switching parse mode only changes WHICH characters are fatal. Escaping is what
    actually closes it, and then the bold title is free."""
    sent = {}

    class R:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None, **kw):
        sent.update(json)
        return R()

    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(ha.httpx, "post", fake_post)

    ha._telegram("Magpie [swing] traded",
                 "BUY BTC/EUR — RSI < 30 & EMA_20 crossed the EMA_50")

    assert sent["parse_mode"] == "HTML"
    body = sent["text"]
    assert "<b>Magpie [swing] traded</b>" in body        # the title is still bold
    assert "RSI &lt; 30 &amp; EMA_20" in body            # the prose is neutralised
    # nothing the model wrote is left as raw markup Telegram would choke on
    assert "< 30" not in body and "& EMA" not in body
