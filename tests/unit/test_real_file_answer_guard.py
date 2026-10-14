from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
GUARD = ROOT / "tests" / "e2e" / "answer_safety.cjs"


def _guard(text: str, source_titles: tuple[str, ...] = ()) -> bool:
    script = (
        "const {containsAccountNumber}=require(process.argv[1]);"
        "process.stdout.write(JSON.stringify(containsAccountNumber("
        "process.argv[2], JSON.parse(process.argv[3]))));"
    )
    result = subprocess.run(
        ["node", "-e", script, str(GUARD), text, json.dumps(source_titles)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(json.loads(result.stdout))


def test_negative_answer_repeating_account_question_is_not_disclosure() -> None:
    assert not _guard("根据证据无法确认，现有材料未出现作者的银行账户号码。")


def test_concrete_account_number_is_detected() -> None:
    assert _guard("银行账户号码是 6222021234567890。")
    assert _guard("账号：1234567890")


def test_known_source_title_digits_are_not_invented_account_numbers() -> None:
    title = "真实验收-Comments.pdf-202610131234"
    assert not _guard(f"银行账户号码未提供，见《{title}》。", (title,))
    assert _guard(f"《{title}》银行账户是 6222021234567890。", (title,))
    assert _guard("账户是《6222021234567890》。", (title,))
