import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from pathlib import Path  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")


def _app():
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_streamlit_chat_answers_from_documents():
    at = _app()
    at.chat_input[0].set_value("What is the dosage of Zentrovax for a 15 kg child?").run()
    assert not at.exception, [e.value for e in at.exception]
    text = " ".join(m.value for cm in at.chat_message for m in cm.markdown)
    assert "100 mg" in text


def test_streamlit_other_pages_render():
    at = _app()
    for page in ("Evals", "Red team", "Knowledge"):
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, [e.value for e in at.exception])
    # run the evaluation on the simulator through the UI
    at.sidebar.radio[0].set_value("Evals").run()
    next(b for b in at.button if b.label == "Run evaluation").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Accuracy" in m.label for m in at.metric)
