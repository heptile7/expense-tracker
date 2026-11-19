"""Offline categorizer: built-in word list, person detection, and learning from your corrections."""

from helpers import MONTH1, hdfc_xlsx

from tracker import db, service
from tracker.importer import import_file
from tracker.local_classifier import build_model, looks_like_person, tokenize


def _model():
    with db.session() as conn:
        return build_model(conn)


def _guess(model, payee, narration="", vpa=None, direction="debit", channel="UPI"):
    g = model.guess(payee, narration, vpa, direction, channel)
    return g.category if g else None


def test_business_words():
    m = _model()
    assert _guess(m, "MAHALAKSHMI MESS") == "Food & Dining"
    assert _guess(m, "KAVITHA TAILORS") == "Personal Care"
    assert _guess(m, "GANESH FLOWER STALL") == "Gifts & Donations"
    assert _guess(m, "SRI BALAJI MEDICALS") == "Health"
    assert _guess(m, "PRAKASH AUTO WORKS") == "Transport"


def test_upi_remark_is_used():
    m = _model()
    assert _guess(m, "SURESH", "UPI-SURESH-SURESH@OKAXIS-UTIB0000001-#-EB BILL") == "Bills & Utilities"


def test_company_credit_by_neft_is_salary():
    m = _model()
    assert _guess(m, "ACME TECHNOLOGIES", "NEFT CR-SBIN#-ACME TECHNOLOGIES PVT LTD-TEST USER", direction="credit", channel="NEFT") == "Salary"


def test_people():
    assert looks_like_person("VIGNESH M", "9840012345@okaxis", "UPI")
    assert looks_like_person("RAMESH KUMAR", "rameshkumar@okaxis", "UPI")
    assert not looks_like_person("KUMAR STORES", "kumarstores@oksbi", "UPI")
    assert not looks_like_person("VIGNESH M", None, "CARD")
    m = _model()
    assert _guess(m, "VIGNESH M", vpa="9840012345@okaxis") == "Payments to People"
    assert _guess(m, "VIGNESH M", vpa="9840012345@okaxis", direction="credit") == "Other Income"


def test_unknown_word_is_not_guessed():
    assert _guess(_model(), "XYZZY QWERTY", channel="CARD") is None


def test_learns_from_your_corrections(tmp_path):
    service.create_account(name="HDFC", bank="HDFC", last4="1234")
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), use_llm=False)
    # "SRI MURUGAN TRADERS" would be a Groceries guess ("TRADERS"); you say it's Shopping.
    murugan = [t for t in service.list_transactions()["items"] if t["counterparty"] == "Sri Murugan Traders"][0]
    service.set_category(murugan["id"], "Shopping")
    # A new, differently-named payee sharing the distinctive word follows your decision.
    assert _guess(_model(), "MURUGAN TRADERS") == "Shopping"


def test_tokenize_ignores_noise_and_handles():
    toks = tokenize("KUMAR STORES", "UPI-KUMAR STORES-KUMARSTORES@OKSBI-SBIN0001234-#-UPI", "kumarstores@oksbi")
    assert {"KUMAR", "STORES", "VPA:KUMARSTORES", "DIR:debit"} <= toks
    assert not {"UPI", "OKSBI", "SBIN"} & toks
