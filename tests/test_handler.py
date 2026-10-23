"""Unit tests for the pure logic in lambda/handler.py (no AWS or Claude calls).

All names, addresses and domains are made up; example.com/.org/.test are reserved for this.
"""

import io
import json
from datetime import date
from email.message import EmailMessage

import pytest

import handler

SES_PASS = (
    "amazonses.com; spf=pass (spfCheck: domain of example.com designates 192.0.2.1 as "
    "permitted sender) smtp.mailfrom=me@example.com; dkim=pass header.i=@example.com; "
    "dmarc=pass header.from=example.com;"
)


def make_email(from_="me@example.com", body="Hello", auth=(SES_PASS,), headers=None, html=None):
    msg = EmailMessage()
    if from_ is not None:
        msg["From"] = from_
    msg["To"] = "parser@example.com"
    msg["Subject"] = "Test"
    for result in auth:
        msg["Authentication-Results"] = result
    for name, value in (headers or {}).items():
        msg[name] = value
    if html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body)
    return msg


# ── sender_is_allowed ──────────────────────────────────────────────────────


def test_allowlisted_authenticated_sender_is_allowed():
    assert handler.sender_is_allowed(make_email())


def test_allowlist_is_case_insensitive():
    assert handler.sender_is_allowed(make_email(from_="Me <ME@Example.com>"))
    partner_pass = SES_PASS.replace("example.com", "example.org")
    assert handler.sender_is_allowed(make_email(from_="partner@example.org", auth=(partner_pass,)))


@pytest.mark.parametrize("from_", ["stranger@example.com", "me@example.com.test", "", None])
def test_sender_not_on_allowlist_is_dropped(from_):
    assert not handler.sender_is_allowed(make_email(from_=from_))


def test_missing_authentication_results_is_dropped():
    assert not handler.sender_is_allowed(make_email(auth=()))


def test_dmarc_fail_is_dropped():
    assert not handler.sender_is_allowed(make_email(auth=(SES_PASS.replace("dmarc=pass", "dmarc=fail"),)))


def test_dmarc_pass_for_another_domain_is_dropped():
    other = SES_PASS.replace("header.from=example.com", "header.from=attacker.test")
    assert not handler.sender_is_allowed(make_email(auth=(other,)))


def test_only_the_first_authentication_results_header_is_trusted():
    failed = SES_PASS.replace("dmarc=pass", "dmarc=fail")
    # A forged passing header further down must not rescue a failed SES verdict.
    assert not handler.sender_is_allowed(make_email(auth=(failed, SES_PASS)))


def test_authentication_results_not_from_ses_is_dropped():
    forged = SES_PASS.replace("amazonses.com;", "mail.attacker.test;")
    assert not handler.sender_is_allowed(make_email(auth=(forged,)))


def test_virus_scan_failure_is_dropped():
    msg = make_email(headers={"X-SES-Virus-Verdict": "FAIL"})
    assert not handler.sender_is_allowed(msg)


# ── find_original_sender ───────────────────────────────────────────────────


def original_sender(msg):
    body = handler.get_body_text(msg)
    return handler.find_original_sender(msg, body, handler.find_attached_email(msg))


def test_forward_as_attachment_uses_attached_sender():
    inner = make_email(from_="Front Office <Office@School.example>", auth=())
    msg = make_email(body="See attached.")
    msg.add_attachment(inner)
    assert original_sender(msg) == "office@school.example"


def test_attached_email_without_from_falls_back_to_body():
    inner = make_email(from_=None, auth=())
    msg = make_email(body="From: Coach <coach@club.example>\nSent: Monday")
    msg.add_attachment(inner)
    assert original_sender(msg) == "coach@club.example"


@pytest.mark.parametrize("body", [
    # Outlook
    "FYI\n\n________________________________\nFrom: Front Office <office@school.example>\n"
    "Sent: Monday, October 19, 2026 9:00 AM\nTo: Me <me@example.com>\nSubject: Picture day",
    # Gmail
    "---------- Forwarded message ---------\nFrom: Front Office <office@school.example>\n"
    "Date: Mon, Oct 19, 2026 at 9:00 AM\nSubject: Picture day",
    # Apple Mail
    "Begin forwarded message:\n\nFrom: Front Office <office@school.example>\n"
    "Subject: Picture day",
    # Outlook plain text with bold markers, and a quoted forward
    "*From:* Front Office <office@school.example>\n*Sent:* Monday",
    "> From: office@school.example\n> Subject: Picture day",
])
def test_forwarded_from_line_formats(body):
    assert original_sender(make_email(body=body)) == "office@school.example"


def test_html_forward():
    html = (
        "<div>FYI</div><hr><div><b>From:</b> Front Office &lt;Office@School.example&gt;<br>"
        "<b>Sent:</b> Monday</div>"
    )
    assert original_sender(make_email(html=html)) == "office@school.example"


def test_first_from_line_wins_in_a_thread():
    body = (
        "From: Coach <coach@club.example>\nSubject: Re: Practice\n\n"
        "From: Parent <parent@elsewhere.example>\nSubject: Practice"
    )
    assert original_sender(make_email(body=body)) == "coach@club.example"


def test_forwarded_from_with_name_only_returns_none():
    body = "From: Front Office\nSent: Monday\nSubject: Picture day"
    assert original_sender(make_email(body=body)) is None


def test_email_that_was_not_forwarded_is_its_own_original():
    msg = make_email(from_="Me <Me@Example.com>", body="Just a note.")
    assert original_sender(msg) == "me@example.com"


# ── Child rules ────────────────────────────────────────────────────────────

ALEX = {"name": "Alex", "domains": ["School.example", "@club.example"], "senders": [" Coach@Gym.example "]}
SAM = {"name": "Sam", "domains": ["school.example"]}


@pytest.mark.parametrize("address, expected", [
    ("office@school.example", ["Alex", "Sam"]),
    ("office@hs.school.example", ["Alex", "Sam"]),
    ("coach@club.example", ["Alex"]),
    ("coach@gym.example", ["Alex"]),
    ("other@gym.example", []),
    ("office@notschool.example", []),
    ("office@school.example.test", []),
])
def test_match_children(address, expected):
    assert [c["name"] for c in handler.match_children([ALEX, SAM], address)] == expected


def test_match_children_without_rules():
    assert handler.match_children([], "office@school.example") == []


class FakeS3:
    def __init__(self, body=None, error=None):
        self.body, self.error = body, error

    def get_object(self, Bucket, Key):
        assert (Bucket, Key) == ("test-bucket", handler.CHILD_RULES_KEY)
        if self.error:
            raise self.error
        return {"Body": io.BytesIO(self.body)}


def rules(*children):
    return FakeS3(json.dumps({"children": list(children)}).encode())


def test_load_valid_child_rules():
    children = [dict(ALEX, graduation_year=2030, activities=["band"]), SAM]
    assert handler.load_child_rules(rules(*children)) == (children, None)


@pytest.mark.parametrize("child", [
    {"domains": ["school.example"]},
    {"name": "  "},
    {"name": "Alex", "domains": "school.example"},
    {"name": "Alex", "activities": "band"},
    {"name": "Alex", "graduation_year": "2030"},
    {"name": "Alex", "graduation_year": True},
    {"name": "Alex", "graduation_year": 30},
])
def test_invalid_child_rules_return_an_error(child):
    children, error = handler.load_child_rules(rules(child))
    assert children == [] and error.startswith("ValueError")


@pytest.mark.parametrize("s3", [FakeS3(error=RuntimeError("no such key")), FakeS3(b"{not json")])
def test_unreadable_child_rules_return_an_error(s3):
    children, error = handler.load_child_rules(s3)
    assert children == [] and error and len(error) <= 300


@pytest.mark.parametrize("graduation_year, today, expected", [
    (2030, date(2026, 10, 22), "9th grade (freshman, Class of 2030)"),
    (2030, date(2026, 7, 31), "8th grade (Class of 2030)"),
    (2030, date(2026, 8, 1), "9th grade (freshman, Class of 2030)"),
    (2028, date(2026, 10, 22), "11th grade (junior, Class of 2028)"),
    (2027, date(2026, 10, 22), "12th grade (senior, Class of 2027)"),
    (2026, date(2026, 10, 22), "graduated (Class of 2026)"),
    (2037, date(2026, 10, 22), "2nd grade (Class of 2037)"),
    (2036, date(2026, 10, 22), "3rd grade (Class of 2036)"),
    (2038, date(2026, 10, 22), "1st grade (Class of 2038)"),
    (2039, date(2026, 10, 22), "kindergarten"),
    (2040, date(2026, 10, 22), "pre-kindergarten"),
])
def test_grade_from_graduation_year(graduation_year, today, expected):
    assert handler.grade_from_graduation_year(graduation_year, today) == expected


def test_hand_set_grade_overrides_graduation_year():
    child = {"name": "Alex", "grade": "7th grade", "graduation_year": 2030}
    assert handler.describe_child(child) == "- Alex; grade: 7th grade"


def test_describe_child_lists_activities_and_senders():
    child = dict(ALEX, activities=["band"], excluded_activities=["soccer"])
    text = handler.describe_child(child, with_senders=True)
    assert "activities: band" in text and "NOT part of: soccer" in text
    assert "sender domains: School.example, @club.example" in text


def test_child_rules_prompt():
    matched = handler.child_rules_prompt([ALEX, SAM], [ALEX], "office@club.example")
    assert "This email is about:\n- Alex" in matched and handler.FILTER_INSTRUCTIONS in matched

    # Sender unknown: Claude gets every child's sender rules and reports which it applied.
    unknown = handler.child_rules_prompt([ALEX, SAM], [], None)
    assert "- Sam" in unknown and '"children" key' in unknown

    assert handler.child_rules_prompt([ALEX, SAM], [], "someone@elsewhere.example") == ""
    assert handler.child_rules_prompt([], [], None) == ""


# ── Summary email ──────────────────────────────────────────────────────────


def test_format_action_items_sorts_dated_items_first():
    items = [
        {"action": "Bring snacks", "due_date": None},
        {"action": "Return form", "due_date": "2026-10-30"},
        "Plain string item",
        {"action": "Pay fee", "due_date": "2026-10-23"},
        {"action": "Bad date", "due_date": "next Friday"},
    ]
    assert handler.format_action_items(items) == [
        "By Friday October 23: Pay fee",
        "By Friday October 30: Return form",
        "Bring snacks",
        "Plain string item",
        "Bad date",
    ]


def test_email_body_escapes_claude_output():
    result = {
        "summary": '<a href="https://attacker.test">Click</a>',
        "action_items": [{"action": "<script>x</script>", "due_date": None}],
        "urgent": True,
    }
    body = handler.build_email_body(result, "parsed-output/x.json", "note <b>")
    assert "<a href" not in body and "<script>" not in body and "note <b>" not in body
    assert "&lt;a href=" in body and "Urgent" in body
