from vklass_mcp.parsers import (
    normalize_calendar_events,
    normalize_news_item,
    parse_account_identity,
    parse_children,
    parse_study_courses,
    parse_weekly_reports,
)


def test_news_weekly_letter_is_normalized_and_secrets_removed() -> None:
    record = normalize_news_item(
        {
            "id": 4716895,
            "title": "Åk 3: Veckobrev v 34",
            "content": "<p>LÄXA TILL MÅNDAG</p><script>bad()</script>",
            "publishDate": "2026-08-20T19:21:00+02:00",
            "token": "secret",
            "files": [{"name": "letter.pdf", "url": "https://files.vklass.se/a.pdf?sig=x"}],
        }
    )
    assert record["kind"] == "weekly_letter"
    assert record["key"] == "4716895"
    assert "bad()" not in record["body_text"]
    assert "token" not in record["data"]
    assert record["data"]["files"][0]["url"] == "https://files.vklass.se/a.pdf"


def test_account_identity_comes_from_stable_vklass_app_data() -> None:
    html = (
        "<script>window['appData'] = "
        '\'{"userId":"guardian-42","userFullName":"Ada Parent"}\';</script>'
    )
    assert parse_account_identity(html) == ("guardian-42", "Ada Parent")


def test_children_are_extracted_and_enriched_with_meal() -> None:
    absence = 'x "fullName":"Alex Example","disabled":false,"value":"12345" y'
    home = """
    <div class="vk-student-card">
      <span class="vk-student-card-header__text">Alex</span>
      <div class="vk-student-card__day__food"><li>Pasta</li></div>
    </div>
    """
    assert parse_children(absence, home) == [
        {
            "id": "12345",
            "name": "Alex Example",
            "home_name": "Alex",
            "meal": ["Pasta"],
        }
    ]


def test_calendar_assignment_event_type_two() -> None:
    records = normalize_calendar_events(
        [
            {
                "id": 8,
                "eventType": 2,
                "title": "Read chapter 4",
                "start": "2026-08-31T08:00:00+02:00",
                "end": "2026-08-31T09:00:00+02:00",
            }
        ],
        "123",
    )
    assert records[0]["kind"] == "assignment"
    assert records[0]["child_id"] == "123"


def test_study_courses_parser() -> None:
    html = """<script>enhanceServerHtml('studyoverview-courses', '',
    '{"items":[{"subjectName":"Matematik","grade":"A","courseActive":true}],
    "inactiveItems":[]}', 'x')</script>"""
    records = parse_study_courses(html, "123")
    assert records[0]["kind"] == "study_course"
    assert records[0]["data"]["grade"] == "A"


def test_weekly_report_parser() -> None:
    html = """
    <vkau-expansion-panel>
      <div slot="expansion-panel-trigger">
        <vkau-icon-badge text="Robin" secondary-text="2026-08-21" />
      </div>
      <div class="legacy-html"><section class="events"><li>Prov måndag</li></section></div>
    </vkau-expansion-panel>
    """
    records = parse_weekly_reports(html)
    assert len(records) == 1
    assert records[0]["kind"] == "weekly_report"
    assert records[0]["data"]["child_name"] == "Robin"
