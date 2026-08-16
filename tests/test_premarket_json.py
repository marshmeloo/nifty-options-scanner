"""
premarket.py's JSON brief output (added 2026-08-16) -- previously only
the rendered markdown was saved, so position_notifier.py's condensed
Telegram summary had no structured brief to read without either
re-running build_brief() a second time or parsing markdown back apart.

Mocks build_brief() itself rather than its many upstream data sources
(Dhan candles, global cues, FII/DII, news, participant OI...) -- this
only tests the NEW write path, not brief-building itself.

Run: python -m pytest tests/test_premarket_json.py -q
"""
import json
from datetime import datetime

import premarket as pm


def _fake_brief():
    return {
        "generated_at": "2026-08-16T09:13:00",
        "bias": "leaning bullish (2/3 signals)",
        "expiry": {"expiry": "2026-08-18", "days_to_expiry": 2, "is_expiry_day": False},
        "event_today": None,
        "news": {"risk": {"level": "normal", "categories_hit": []}},
    }


def test_brief_json_path_uses_todays_date():
    path = pm.brief_json_path(datetime(2026, 8, 16, 9, 0))
    assert path.name == "premarket_brief_20260816.json"


def test_main_writes_json_matching_the_markdown_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "LOG_DIR", tmp_path)
    fake = _fake_brief()
    monkeypatch.setattr(pm, "build_brief", lambda: fake)
    monkeypatch.setattr(pm, "render_markdown", lambda brief: "fake markdown")

    pm.main()

    json_path = pm.brief_json_path()
    assert json_path.exists()
    saved = json.loads(json_path.read_text())
    assert saved == fake

    md_path = tmp_path / f"premarket_brief_{datetime.now().strftime('%Y%m%d')}.md"
    assert md_path.exists()
    assert md_path.read_text() == "fake markdown"


def test_main_json_output_is_readable_back_with_the_same_types(tmp_path, monkeypatch):
    """Round-trips through json.dumps(default=str) -- confirms nothing in a
    realistic brief shape breaks serialization silently."""
    monkeypatch.setattr(pm, "LOG_DIR", tmp_path)
    fake = _fake_brief()
    fake["levels"] = [{"kind": "support", "low": 24000.0, "high": 24050.0}]
    fake["fii_dii"] = {"fii_net": -510.7, "dii_net": 4353.1}
    monkeypatch.setattr(pm, "build_brief", lambda: fake)
    monkeypatch.setattr(pm, "render_markdown", lambda brief: "fake markdown")

    pm.main()

    saved = json.loads(pm.brief_json_path().read_text())
    assert saved["levels"][0]["low"] == 24000.0
    assert saved["fii_dii"]["fii_net"] == -510.7
