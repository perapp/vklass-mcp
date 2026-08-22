"""Tolerant parsers for Vklass' undocumented guardian web responses."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

_WS = re.compile(r"\s+")
_WEEKLY = re.compile(r"\bvecko(?:brev|rapport)|weekly\s+(?:letter|report)\b", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"\b(läx\w*|prov\w*|uppgift\w*|inlämn\w*|förhör\w*|test\w*|exam\w*)\b",
    re.IGNORECASE,
)
_SECRET_KEY = re.compile(r"cookie|token|authorization|saml|signature|relaystate", re.I)


def parse_account_identity(html: str) -> tuple[str, str]:
    """Extract the stable guardian user ID and display name from Vklass appData."""

    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False)
        if "appData" not in text:
            continue
        match = re.search(r"window\[['\"]appData['\"]\]\s*=\s*(['\"])(.+?)\1\s*;?", text)
        if not match:
            continue
        raw = match.group(2).replace(r"\'", "'").replace(r"\"", '"')
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        user_id = str(data.get("userId") or "").strip()
        display_name = str(data.get("userFullName") or "").strip()
        if user_id and display_name:
            return user_id, display_name
    raise ValueError("Vklass account identity was not present in appData")


def plain_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = str(value)
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for element in soup(["script", "style", "noscript"]):
            element.decompose()
        text = soup.get_text("\n")
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    if limit is not None and len(text) > limit:
        return text[:limit] + "…"
    return text


def html_to_text(value: Any) -> str:
    return plain_text(value)


def parse_children(absence_html: str, home_html: str = "") -> list[dict[str, Any]]:
    """Extract guardian wards from the absence page and enrich from home cards."""

    found: dict[str, dict[str, Any]] = {}
    patterns = (
        re.compile(
            r'"fullName"\s*:\s*"(?P<name>[^"]+)"[^}]{0,500}?"value"\s*:\s*"(?P<id>\d{3,})"',
            re.DOTALL,
        ),
        re.compile(
            r'"value"\s*:\s*"(?P<id>\d{3,})"[^}]{0,500}?"fullName"\s*:\s*"(?P<name>[^"]+)"',
            re.DOTALL,
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(absence_html):
            child_id = match.group("id")
            found[child_id] = {
                "id": child_id,
                "name": unescape(match.group("name")),
            }

    soup = BeautifulSoup(home_html, "html.parser")
    cards: list[dict[str, Any]] = []
    for card_tag in soup.find_all("div", class_=lambda value: value and "vk-student-card" in value):
        name_element = card_tag.find(
            class_=lambda value: value and "vk-student-card-header__text" in value
        )
        name = plain_text(name_element.get_text(" ") if name_element else "")
        if not name:
            continue
        meal_items = [
            plain_text(item.get_text(" "))
            for item in card_tag.select(".vk-student-card__day__food li")
        ]
        cards.append({"name": name, "meal": [item for item in meal_items if item]})

    for child in found.values():
        for card_data in cards:
            if card_data["name"] in child["name"] or child["name"] in card_data["name"]:
                child["home_name"] = card_data["name"]
                child["meal"] = card_data["meal"]
                break
    return sorted(found.values(), key=lambda item: str(item.get("name", "")))


def parse_children_authoritative(
    absence_html: str, home_html: str = ""
) -> tuple[list[dict[str, Any]], bool]:
    """Parse wards and report whether an explicit Vklass ward list was validated."""

    children = parse_children(absence_html, home_html)
    payload = _aurelia_payload(
        absence_html,
        component="absence-notify",
        view="common/views/absence/absence-notify",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("studentOptions"), list):
        return children, False
    expected_ids: set[str] = set()
    for item in payload["studentOptions"]:
        if not isinstance(item, dict):
            return children, False
        child_id = str(item.get("value") or "").strip()
        full_name = str(item.get("fullName") or "").strip()
        if not child_id.isdigit() or not full_name:
            return children, False
        expected_ids.add(child_id)
    actual_ids = {str(child["id"]) for child in children}
    return children, actual_ids == expected_ids


def normalize_news_items(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(payload, list):
        items = payload
        next_page = None
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("news") or payload.get("articles") or []
        next_page = _first(payload, "nextPageToken", "next_page_token", "continuationToken")
    else:
        return [], None
    records = [normalize_news_item(item) for item in items if isinstance(item, dict)]
    return records, str(next_page) if next_page else None


def normalize_news_item(item: dict[str, Any]) -> dict[str, Any]:
    title = plain_text(_first(item, "title", "name", "subject", "heading"))
    body_html = str(_first(item, "body", "content", "html", "text", "description") or "")
    body_text = html_to_text(body_html)
    source_date = _iso_date(
        _first(item, "publishDate", "publishedAt", "createdAt", "date", "publish_date", "updatedAt")
    )
    identifier = _first(item, "id", "articleId", "newsArticleId", "guid", "uuid", "fileName")
    if identifier is None:
        identifier = hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()[:24]
    audience = plain_text(
        _first(item, "audience", "target", "groups", "groupNames", "recipients", "contexts"), 500
    )
    return {
        "kind": "weekly_letter" if _WEEKLY.search(f"{title}\n{body_text[:500]}") else "news",
        "key": str(identifier),
        "title": title,
        "audience": audience,
        "body_text": body_text,
        "body_html": body_html,
        "source_updated_at": source_date,
        "data": _sanitize_raw(item),
    }


def parse_weekly_reports(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []
    for index, panel in enumerate(soup.find_all("vkau-expansion-panel")):
        trigger = panel.select_one('[slot="expansion-panel-trigger"]')
        content = panel.find("div", class_="legacy-html")
        if trigger is None or content is None:
            continue
        badge = trigger.find("vkau-icon-badge")
        child_name = str(badge.get("text", "")) if badge else ""
        report_date = str(badge.get("secondary-text", "")) if badge else ""
        body = plain_text(content.get_text("\n"))
        links = []
        for anchor in content.find_all("a", href=True):
            href = str(anchor["href"])
            links.append(
                {"label": plain_text(anchor.get_text(" ")), "url": _url_without_query(href)}
            )
        key_material = f"{child_name}|{report_date}|{index}|{body[:100]}"
        records.append(
            {
                "kind": "weekly_report",
                "key": hashlib.sha256(key_material.encode()).hexdigest()[:24],
                "title": f"Veckorapport {report_date}".strip(),
                "audience": child_name,
                "body_text": body,
                "source_updated_at": _iso_date(report_date),
                "data": {"child_name": child_name, "links": links},
            }
        )
    return records


def normalize_calendar_events(payload: Any, child_id: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    records: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        title = plain_text(_first(item, "title", "name", "summary", "text"))
        description = html_to_text(_first(item, "text", "description", "details"))
        start = _iso_date(_first(item, "start", "startDate", "from"))
        end = _iso_date(_first(item, "end", "endDate", "to"))
        event_type = _first(item, "eventType", "type", "event_type")
        detail_url = str(_first(item, "detailUrl", "url", "href") or "")
        identifier = _first(item, "id", "uid", "eventId") or detail_url
        if not identifier:
            identifier = hashlib.sha256(
                f"{child_id}|{start}|{end}|{title}|{index}".encode()
            ).hexdigest()[:24]
        kind = (
            "assignment"
            if str(event_type) == "2" or _ASSIGNMENT.search(f"{title} {description}")
            else "calendar"
        )
        records.append(
            {
                "kind": kind,
                "key": f"{child_id}:{identifier}",
                "child_id": child_id,
                "title": title,
                "start_at": start,
                "end_at": end,
                "body_text": description,
                "data": {
                    "event_type": event_type,
                    "location": plain_text(item.get("location")),
                    "cancelled": bool(item.get("cancelled", False)),
                    "context": plain_text(item.get("context")),
                    "detail_path": _url_without_query(detail_url),
                },
            }
        )
    return records


def validate_care_schedule_payload(payload: Any) -> None:
    """Reject schema drift before an authoritative care-schedule cache replacement."""

    if not isinstance(payload, dict) or not isinstance(payload.get("scheduleData"), list):
        raise ValueError("care schedule response had an unexpected schema")
    response_start = _care_date(payload.get("fromDate"))
    response_end = _care_date(payload.get("untilDate"))
    if not response_start or not response_end or response_end < response_start:
        raise ValueError("care schedule response had an invalid date window")
    for item in payload["scheduleData"]:
        if not isinstance(item, dict):
            raise ValueError("care schedule response contained an invalid day")
        child_id = str(item.get("studentId") or "").strip()
        school_id = str(item.get("schoolId") or "").strip()
        schedule_date = _care_date(item.get("date"))
        if not child_id or not school_id or not schedule_date:
            raise ValueError("care schedule response contained an invalid day identity")
        if not response_start <= schedule_date <= response_end:
            raise ValueError("care schedule response contained a day outside its date window")
        for key in ("startTime", "endTime", "dropOffTime", "pickUpTime"):
            if item.get(key) not in (None, "") and _care_time(item[key]) is None:
                raise ValueError("care schedule response contained an invalid time")
        for key in ("absenceStart", "absenceEnd"):
            if item.get(key) not in (None, "") and _care_date(item[key]) is None:
                raise ValueError("care schedule response contained an invalid absence date")
        if not _is_care_bool(item.get("isOnLeave")):
            raise ValueError("care schedule response contained an invalid leave value")
    for key in ("schoolClosedData", "schoolHolidayData"):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError("care schedule response contained invalid closure data")
        for dates in value.values():
            if not isinstance(dates, list) or any(_care_date(item) is None for item in dates):
                raise ValueError("care schedule response contained invalid closure dates")


def normalize_care_schedule(payload: Any) -> list[dict[str, Any]]:
    """Normalize the read-only care schedule returned by ``CareSchedule/LoadDays``."""

    validate_care_schedule_payload(payload)
    closed_dates = _dates_by_school(payload.get("schoolClosedData"))
    holiday_dates = _dates_by_school(payload.get("schoolHolidayData"))
    records: list[dict[str, Any]] = []
    for item in payload["scheduleData"]:
        if not isinstance(item, dict):
            continue
        child_id = str(item.get("studentId") or "").strip()
        school_id = str(item.get("schoolId") or "").strip()
        schedule_date = str(item.get("date") or "").strip()
        if not child_id or not school_id or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", schedule_date):
            continue
        try:
            date.fromisoformat(schedule_date)
        except ValueError:
            continue

        start_time = _care_time(item.get("startTime"))
        end_time = _care_time(item.get("endTime"))
        drop_off_time = _care_time(item.get("dropOffTime"))
        pick_up_time = _care_time(item.get("pickUpTime"))
        message = plain_text(item.get("message"), 1000)
        is_on_leave = _care_bool(item.get("isOnLeave"))
        school_closed = schedule_date in closed_dates.get(school_id, set())
        school_holiday = schedule_date in holiday_dates.get(school_id, set())
        absence_start = _care_date(item.get("absenceStart"))
        absence_end = _care_date(item.get("absenceEnd"))
        if not any(
            (
                start_time,
                end_time,
                drop_off_time,
                pick_up_time,
                message,
                is_on_leave,
                school_closed,
                school_holiday,
                absence_start,
                absence_end,
            )
        ):
            continue

        details = {
            "date": schedule_date,
            "planned_start_time": start_time,
            "planned_end_time": end_time,
            "actual_drop_off_time": drop_off_time,
            "actual_pick_up_time": pick_up_time,
            "is_on_leave": is_on_leave,
            "school_closed": school_closed,
            "school_holiday": school_holiday,
            "absence_start": absence_start,
            "absence_end": absence_end,
            "message": message or None,
        }
        body_parts = []
        if start_time or end_time:
            body_parts.append(f"Planned care: {start_time or '?'}–{end_time or '?'}")
        if drop_off_time or pick_up_time:
            body_parts.append(f"Actual attendance: {drop_off_time or '?'}–{pick_up_time or '?'}")
        if is_on_leave:
            body_parts.append("On leave")
        if school_closed:
            body_parts.append("Care facility closed")
        if school_holiday:
            body_parts.append("School holiday")
        if message:
            body_parts.append(message)
        start_at = _care_datetime(schedule_date, start_time)
        end_at = _care_datetime(schedule_date, end_time) if end_time else None
        if end_at and start_time and end_at < start_at:
            end_at = (datetime.fromisoformat(end_at) + timedelta(days=1)).isoformat()
        school_digest = hashlib.sha256(school_id.encode()).hexdigest()[:8]
        records.append(
            {
                "kind": "care_schedule",
                "key": f"{child_id}:{school_digest}:{schedule_date}",
                "child_id": child_id,
                "title": "Omsorgsschema",
                "start_at": start_at,
                "end_at": end_at,
                "body_text": "\n".join(body_parts),
                "data": details,
            }
        )
    return records


def parse_study_courses(html: str, child_id: str) -> list[dict[str, Any]]:
    """Extract normalized course/judgement items from the embedded study-overview JSON."""

    match = re.search(
        r"enhanceServerHtml\('studyoverview-courses',\s*'',\s*'(\{.*?\})\s*',\s*'",
        html,
        re.DOTALL,
    )
    if not match:
        return []
    encoded = unescape(match.group(1)).replace("\\'", "'")
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError:
        return []
    items = [*(payload.get("items") or []), *(payload.get("inactiveItems") or [])]
    records: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        subject = plain_text(item.get("subjectName") or item.get("courseName"))
        course = plain_text(item.get("courseNameAndCourseCode"))
        judgement = plain_text(item.get("judgement"))
        grade = plain_text(item.get("grade"))
        source_date = _iso_date(item.get("date"))
        identifier = item.get("id") or item.get("courseId")
        if not identifier:
            identifier = hashlib.sha256(
                f"{child_id}|{subject}|{course}|{index}".encode()
            ).hexdigest()[:24]
        records.append(
            {
                "kind": "study_course",
                "key": f"{child_id}:{identifier}",
                "child_id": child_id,
                "title": subject or course,
                "body_text": "\n".join(
                    value
                    for value in (
                        course,
                        f"Omdöme: {judgement}" if judgement else "",
                        f"Betyg: {grade}" if grade else "",
                    )
                    if value
                ),
                "source_updated_at": source_date,
                "data": {
                    "course": course,
                    "judgement": judgement,
                    "grade": grade or None,
                    "active": bool(item.get("courseActive", True)),
                },
            }
        )
    return records


def selected_school_id(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", {"id": "SchoolId"})
    if select is None:
        return None
    selected = select.find("option", selected=True)
    if selected is None:
        return None
    value = selected.get("value")
    return str(value) if value else None


def snapshot_record(kind: str, key: str, html_or_text: str, title: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "key": key,
        "title": title,
        "body_text": plain_text(html_or_text),
        "data": {},
    }


def is_assignment(record: dict[str, Any]) -> bool:
    return record.get("kind") == "assignment" or bool(
        _ASSIGNMENT.search(f"{record.get('title', '')} {record.get('body_text', '')}")
    )


def _aurelia_payload(html: str, *, component: str, view: str) -> Any:
    soup = BeautifulSoup(html, "html.parser")
    marker = re.compile(rf"enhanceServerHtml\('{re.escape(component)}',\s*'{re.escape(view)}',\s*")
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False)
        match = marker.search(text)
        if not match or match.end() >= len(text) or text[match.end()] != "'":
            continue
        start = match.end()
        escaped = False
        for index in range(start + 1, len(text)):
            character = text[index]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "'":
                try:
                    encoded = ast.literal_eval(text[start : index + 1])
                    return json.loads(encoded)
                except (SyntaxError, TypeError, ValueError, json.JSONDecodeError):
                    break
    return None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _iso_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, ZoneInfo("Europe/Stockholm"))
    else:
        try:
            parsed = date_parser.parse(str(value), dayfirst=True)
        except (ValueError, TypeError, OverflowError):
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Stockholm"))
    return parsed.astimezone(UTC).isoformat()


def _care_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", str(value).strip())
    if not match:
        return None
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _care_date(value: Any) -> str | None:
    candidate = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return None
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _is_care_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return value in (0, 1)
    return str(value or "").strip().casefold() in {"true", "false", "0", "1"}


def _care_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().casefold() in {"true", "1"}


def _care_datetime(day: str, clock: str | None) -> str:
    parsed_date = date.fromisoformat(day)
    parsed_time = time.fromisoformat(clock or "00:00")
    value = datetime.combine(parsed_date, parsed_time, ZoneInfo("Europe/Stockholm"))
    return value.astimezone(UTC).isoformat()


def _dates_by_school(value: Any) -> dict[str, set[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, set[str]] = {}
    for school_id, dates in value.items():
        if not isinstance(dates, list):
            continue
        normalized = {_care_date(item) for item in dates}
        result[str(school_id)] = {item for item in normalized if item}
    return result


def _sanitize_raw(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_raw(child)
            for key, child in value.items()
            if not _SECRET_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_raw(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://", "webcal://")):
        return _url_without_query(value)
    return value


def _url_without_query(value: str) -> str:
    return value.split("?", 1)[0] if value else ""


def titles(records: Iterable[dict[str, Any]]) -> list[str]:
    return [str(record.get("title") or "") for record in records]
