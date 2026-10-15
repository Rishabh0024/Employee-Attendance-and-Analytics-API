"""HROne Employee Attendance & Analytics API (contract v2.0).

Run from the repository root:
    uvicorn app.main:app --port 8000

All API/business logic is intentionally kept in this file, as required by the
candidate kit. MongoDB stores instants as UTC BSON datetimes and calendar
values as YYYY-MM-DD / YYYY-MM strings.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Real process environment variables take priority over local .env values.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("hrone.attendance")

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=5_000,
    connectTimeoutMS=5_000,
    socketTimeoutMS=15_000,
    appname="HROneAttendanceAPI",
)
db = client[MONGO_DB]

_indexes_ready = False
_indexes_lock = threading.Lock()


def ensure_indexes() -> None:
    """Create the query/uniqueness indexes idempotently."""
    global _indexes_ready
    if _indexes_ready:
        return
    with _indexes_lock:
        if _indexes_ready:
            return
        db.employees.create_index(
            [("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code"
        )
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
            name="ix_employees_department_joined_emp",
        )
        db.employees.create_index(
            [("department", ASCENDING), ("emp_code", ASCENDING)],
            name="ix_employees_department_emp",
        )
        db.employees.create_index(
            [("joined_on", ASCENDING), ("department", ASCENDING)],
            name="ix_employees_joined_department",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="uq_attendance_emp_date",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
            name="ix_attendance_emp_punchin",
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="ix_attendance_date_emp",
        )
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
            name="ix_attendance_status_date_emp",
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
            name="ix_attendance_date_late_emp",
        )
        _indexes_ready = True
        logger.info("MongoDB indexes are ready for database %s", MONGO_DB)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Do not prevent the process from starting when MongoDB is temporarily down;
    # /health reports 503 and retries index creation when MongoDB returns.
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        logger.exception("MongoDB unavailable during startup; health check will retry")
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)
# Match the supplied OpenAPI contract version.
app.openapi_version = "3.0.3"


# --------------------------------------------------------------------------- #
# Request models and validation
# --------------------------------------------------------------------------- #
class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(APIModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @field_validator("joined_on", mode="before")
    @classmethod
    def require_calendar_date_string(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must be a YYYY-MM-DD string")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid calendar date") from exc
        return value

    @model_validator(mode="after")
    def validate_shift(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(APIModel):
    emp_code: str
    punched_at: StrictInt = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"



class PunchOutIn(APIModel):
    emp_code: str
    punched_at: StrictInt = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)



class RegularizeIn(APIModel):
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] = None
    punch_in: StrictInt = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: StrictInt = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)



# --------------------------------------------------------------------------- #
# Shared utility functions
# --------------------------------------------------------------------------- #
def parse_calendar_date(value: str, field_name: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field_name} must be a valid calendar date")


def parse_month(value: str) -> tuple[date, date, str, str]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    try:
        first = date(year, month, 1)
        if month == 12:
            next_first = date(year + 1, 1, 1)
        else:
            next_first = date(year, month + 1, 1)
    except ValueError:
        raise HTTPException(422, "month must be a valid calendar month")
    last = next_first - timedelta(days=1)
    return first, last, first.isoformat(), next_first.isoformat()


def parse_shift_time(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise HTTPException(422, "shift time must be HH:MM")
    hour, minute = map(int, value.split(":"))
    return hour, minute


def to_utc(value: datetime) -> datetime:
    """PyMongo may return naive datetimes; stored BSON dates are UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_seconds(value: datetime) -> datetime:
    return to_utc(value).replace(microsecond=0)


def epoch_ms_to_datetime(value: Optional[int]) -> datetime:
    if value is None:
        return truncate_seconds(datetime.now(UTC))
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value")
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp is outside the supported epoch-millisecond range")
    try:
        return truncate_seconds(datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value))
    except (OverflowError, ValueError):
        raise HTTPException(422, "timestamp is invalid")


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    instant = to_utc(value).replace(microsecond=0)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return int((instant - epoch).total_seconds() * 1000)


def attendance_date_for(punch_in: datetime, employee: dict) -> date:
    """R1: IST date, adjusted to previous shift-start date for overnight shifts."""
    local = to_utc(punch_in).astimezone(IST)
    start = employee["shift_start"]
    end = employee["shift_end"]
    local_day = local.date()
    if end <= start and local.strftime("%H:%M") < end:
        return local_day - timedelta(days=1)
    return local_day


def shift_start_datetime(attendance_day: date, shift_start: str) -> datetime:
    hour, minute = parse_shift_time(shift_start)
    return datetime.combine(attendance_day, time(hour, minute), tzinfo=IST)


def shift_end_datetime(attendance_day: date, employee: dict) -> datetime:
    start_text = employee["shift_start"]
    end_text = employee["shift_end"]
    hour, minute = parse_shift_time(end_text)
    end_value = datetime.combine(attendance_day, time(hour, minute), tzinfo=IST)
    if end_text <= start_text:
        end_value += timedelta(days=1)
    return end_value


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: date) -> int:
    punch_local = to_utc(punch_in).astimezone(IST)
    scheduled_start = shift_start_datetime(attendance_day, shift_start)
    elapsed_seconds = (punch_local - scheduled_start).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return max(0, int(elapsed_seconds // 60))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((to_utc(punch_out) - to_utc(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, employee: dict, attendance_day: date) -> int:
    end_utc = shift_end_datetime(attendance_day, employee).astimezone(UTC)
    elapsed_seconds = (to_utc(punch_out) - end_utc).total_seconds()
    whole_minutes = int(elapsed_seconds // 60)
    return whole_minutes if whole_minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and Decimal(str(work_hours)) < Decimal("4.50")


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def serialize_history(history: Optional[list]) -> list:
    output = []
    for item in history or []:
        changes = {}
        for field_name, values in (item.get("changes") or {}).items():
            old_value, new_value = values.get("from"), values.get("to")
            if field_name in ("punch_in", "punch_out"):
                old_value = datetime_to_epoch_ms(old_value) if old_value is not None else None
                new_value = datetime_to_epoch_ms(new_value) if new_value is not None else None
            changes[field_name] = {"from": old_value, "to": new_value}
        output.append({
            "at": datetime_to_epoch_ms(item.get("at")),
            "by": item.get("by", ""),
            "reason": item.get("reason", ""),
            "changes": changes,
        })
    return output


def serialize_attendance(document: dict) -> dict:
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": datetime_to_epoch_ms(document.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": int(document.get("late_minutes", 0) or 0),
        "overtime_minutes": int(document.get("overtime_minutes", 0) or 0),
        "half_day": bool(document.get("half_day", False)),
        "history": serialize_history(document.get("history", [])),
    }


def serialize_employee(document: dict) -> dict:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": datetime_to_epoch_ms(document["created_at"]),
    }


def _month_date_datetime(day_value: date) -> datetime:
    return datetime.combine(day_value, time.min, tzinfo=UTC)


def _date_as_datetime_expression(date_field: str) -> dict:
    return {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d", "timezone": "UTC"}}


def _weekday_expression(date_field: str) -> dict:
    return {"$in": [{"$dayOfWeek": _date_as_datetime_expression(date_field)}, [2, 3, 4, 5, 6]]}


def _working_days_expression(joined_on_field: str, month_start: date, month_end: date) -> dict:
    """Mongo expression for inclusive weekdays from max(joined_on, month start) to month end."""
    start_date_expr = {
        "$max": [
            _date_as_datetime_expression(joined_on_field),
            _month_date_datetime(month_start),
        ]
    }
    end_date = _month_date_datetime(month_end)
    return {
        "$let": {
            "vars": {"startDate": start_date_expr},
            "in": {
                "$let": {
                    "vars": {
                        "dayCount": {
                            "$add": [
                                {"$dateDiff": {"startDate": "$$startDate", "endDate": end_date, "unit": "day"}},
                                1,
                            ]
                        },
                        "startDow": {"$dayOfWeek": "$$startDate"},
                    },
                    "in": {
                        "$cond": [
                            {"$lte": ["$$dayCount", 0]},
                            0,
                            {
                                "$add": [
                                    {"$multiply": [{"$floor": {"$divide": ["$$dayCount", 7]}}, 5]},
                                    {
                                        "$size": {
                                            "$filter": {
                                                "input": {"$range": [0, {"$mod": ["$$dayCount", 7]}]},
                                                "as": "offset",
                                                "cond": {
                                                    "$in": [
                                                        {"$add": [
                                                            {"$mod": [{"$add": ["$$startDow", "$$offset", -1]}, 7]},
                                                            1,
                                                        ]},
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            }
                                        }
                                    },
                                ]
                            },
                        ]
                    },
                }
            },
        }
    }


def _sum_map_filtered(array_field: str, condition: dict, mapped_value: dict) -> dict:
    return {
        "$sum": {
            "$map": {
                "input": {"$filter": {"input": array_field, "as": "log", "cond": condition}},
                "as": "log",
                "in": mapped_value,
            }
        }
    }


def _size_filtered(array_field: str, condition: dict) -> dict:
    return {"$size": {"$filter": {"input": array_field, "as": "log", "cond": condition}}}


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    month_start, month_end, start_text, next_text = parse_month(month)
    present_cond = {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}
    present_workday_cond = {"$and": [present_cond, _weekday_expression("$$log.date"), {"$gte": ["$$log.date", "$joined_on"]}]}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employeeCode": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employeeCode"]},
                    {"$gte": ["$date", start_text]},
                    {"$lt": ["$date", next_text]},
                ]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$set": {
            "working_days": _working_days_expression("$joined_on", month_start, month_end),
            "present_days": _sum_map_filtered(
                "$logs", present_workday_cond,
                {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1.0]},
            ),
            "leave_days": _size_filtered("$logs", {"$eq": ["$$log.status", "LEAVE"]}),
            "late_count": _size_filtered("$logs", {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}),
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.overtime_minutes", 0]}}}},
        }},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "month": {"$literal": month},
            "working_days": 1,
            "present_days": 1,
            "leave_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "total_overtime_minutes": 1,
            "attendance_pct": {"$cond": [
                {"$gt": ["$working_days", 0]},
                {"$toDouble": {"$divide": [
                    {"$floor": {"$add": [
                        {"$multiply": [{"$divide": [{"$toDecimal": "$present_days"}, {"$toDecimal": "$working_days"}]}, Decimal128("1000000")]},
                        Decimal128("0.5"),
                    ]}},
                    Decimal128("10000"),
                ]}},
                None,
            ]},
        }},
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    month_start, month_end, start_text, next_text = parse_month(month)
    employee_match: dict = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    present_cond = {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}
    present_workday_cond = {"$and": [present_cond, _weekday_expression("$$log.date"), {"$gte": ["$$log.date", "$joined_on"]}]}
    eligible_hours_cond = {"$and": [present_cond, {"$ne": [{"$ifNull": ["$$log.work_hours", None]}, None]}]}
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employeeCode": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employeeCode"]},
                    {"$gte": ["$date", start_text]},
                    {"$lt": ["$date", next_text]},
                ]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$set": {
            "employee_present_days": _sum_map_filtered(
                "$logs", present_workday_cond,
                {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1.0]},
            ),
            "employee_hour_sum": {"$sum": {"$map": {"input": {"$filter": {"input": "$logs", "as": "log", "cond": eligible_hours_cond}}, "as": "log", "in": {"$toDecimal": "$$log.work_hours"}}}},
            "employee_hour_count": _size_filtered("$logs", eligible_hours_cond),
            "employee_late_count": _size_filtered("$logs", {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}),
            "employee_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "employee_leave_count": _size_filtered("$logs", {"$eq": ["$$log.status", "LEAVE"]}),
            "employee_on_duty_count": _size_filtered("$logs", {"$eq": ["$$log.status", "ON_DUTY"]}),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$employee_present_days"},
            "work_hours_sum": {"$sum": "$employee_hour_sum"},
            "work_hours_count": {"$sum": "$employee_hour_count"},
            "late_count": {"$sum": "$employee_late_count"},
            "total_late_minutes": {"$sum": "$employee_late_minutes"},
            "leave_count": {"$sum": "$employee_leave_count"},
            "on_duty_count": {"$sum": "$employee_on_duty_count"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$toDouble": {"$divide": [
                    {"$floor": {"$add": [
                        {"$multiply": [{"$divide": ["$work_hours_sum", {"$toDecimal": "$work_hours_count"}]}, Decimal128("100")]},
                        Decimal128("0.5"),
                    ]}},
                    Decimal128("100"),
                ]}},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list[dict]:
    _, _, start_text, next_text = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_text, "$lt": next_text}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: date, to_date: date) -> list[dict]:
    day_count = (to_date - from_date).days + 1
    from_ist_midnight = datetime.combine(from_date, time.min, tzinfo=IST).astimezone(UTC)
    to_exclusive_ist = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=IST).astimezone(UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {
            "_id": "$department",
            "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "employees": 1,
            "days": {"$map": {
                "input": {"$range": [0, day_count]},
                "as": "offset",
                "in": {"$dateAdd": {"startDate": from_ist_midnight, "unit": "day", "amount": "$$offset"}},
            }},
        }},
        {"$unwind": "$days"},
        {"$set": {"date_str": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}},
        {"$set": {
            "daily_employees": {"$filter": {"input": "$employees", "as": "employee", "cond": {"$lte": ["$$employee.joined_on", "$date_str"]}}}
        }},
        {"$set": {
            "headcount": {"$size": "$daily_employees"},
            "daily_codes": {"$map": {"input": "$daily_employees", "as": "employee", "in": "$$employee.emp_code"}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "date_str",
            "foreignField": "date",
            "let": {"employeeCodes": "$daily_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$in": ["$emp_code", "$$employeeCodes"]}}},
                {"$project": {"_id": 0, "status": 1, "late_minutes": 1, "half_day": 1}},
            ],
            "as": "logs",
        }},
        # The date scaffold already contains every requested date. $densify is
        # retained as a database-side gap-fill guarantee, not a Python date loop.
        {"$densify": {"field": "days", "range": {"step": 1, "unit": "day", "bounds": [from_ist_midnight, to_exclusive_ist]}}},
        {"$set": {
            "is_working_day": {"$in": [{"$dayOfWeek": {"date": "$days", "timezone": "Asia/Kolkata"}}, [2, 3, 4, 5, 6]]},
            "present_count": _sum_map_filtered(
                "$logs", {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1.0]},
            ),
            "late_count": _size_filtered("$logs", {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}),
        }},
        {"$set": {
            "attendance_rate_raw": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]},
                None,
            ]}
        }},
        {"$setWindowFields": {
            "sortBy": {"days": 1},
            "output": {"moving_avg_raw": {
                "$avg": "$attendance_rate_raw",
                "window": {"documents": [-6, "current"]},
            }},
        }},
        {"$project": {
            "_id": 0,
            "department": 1,
            "date": "$date_str",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [
                {"$ne": ["$attendance_rate_raw", None]},
                {"$toDouble": {"$divide": [
                    {"$floor": {"$add": [{"$multiply": ["$attendance_rate_raw", Decimal128("10000")]}, Decimal128("0.5")]}},
                    Decimal128("10000"),
                ]}},
                None,
            ]},
            "moving_avg_7d": {"$cond": [
                {"$ne": ["$moving_avg_raw", None]},
                {"$toDouble": {"$divide": [
                    {"$floor": {"$add": [{"$multiply": ["$moving_avg_raw", Decimal128("10000")]}, Decimal128("0.5")]}},
                    Decimal128("10000"),
                ]}},
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


def _explain_collection(collection_name: str, pipeline: Optional[list[dict]] = None, find_command: Optional[dict] = None) -> dict:
    if find_command is not None:
        command = find_command
    else:
        command = {"aggregate": collection_name, "pipeline": pipeline or [], "cursor": {}, "allowDiskUse": True}
    return db.command("explain", command, verbosity="executionStats")


def _json_safe(value: dict) -> dict:
    return json_util.loads(json_util.dumps(value))


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    try:
        client.admin.command("ping")
        ensure_indexes()
        return {"status": "ok"}
    except PyMongoError as exc:
        logger.warning("MongoDB readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict:
    document = body.model_dump()
    document["joined_on"] = body.joined_on.isoformat()
    document["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        result = db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError:
        logger.exception("Employee creation failed")
        raise HTTPException(status_code=503, detail="Database operation failed")
    document["_id"] = result.inserted_id
    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: str = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    query = {} if department is None else {"department": department}
    try:
        total = db.employees.count_documents(query)
        documents = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
        items = []
        for document in documents:
            # Employee list response has exactly the Employee schema fields.
            created_at = document.get("created_at")
            items.append({
                "emp_code": document["emp_code"], "name": document["name"],
                "email": document["email"], "department": document["department"],
                "shift_start": document["shift_start"], "shift_end": document["shift_end"],
                "joined_on": document["joined_on"], "created_at": datetime_to_epoch_ms(created_at),
            })
        return {"items": items, "total": total, "page": page, "page_size": page_size}
    except PyMongoError:
        logger.exception("Employee listing failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict:
    try:
        employee = db.employees.find_one({"emp_code": body.emp_code})
        if employee is None:
            raise HTTPException(status_code=404, detail="unknown employee")
        punched_at = epoch_ms_to_datetime(body.punched_at)
        attendance_day = attendance_date_for(punched_at, employee)
        document = {
            "emp_code": body.emp_code,
            "date": attendance_day.isoformat(),
            "status": body.status,
            "punch_in": punched_at,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], attendance_day),
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        # Unique (emp_code, date) index is the concurrency arbiter. Never rely
        # on a check-then-insert sequence.
        db.attendance_logs.insert_one(document)
        return serialize_attendance(document)
    except HTTPException:
        raise
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    except PyMongoError:
        logger.exception("Punch-in failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


def build_attendance_query(
    emp_code: Optional[str], date_from: Optional[date], date_to: Optional[date], status: Optional[str]
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_filter: dict = {}
        if date_from is not None:
            date_filter["$gte"] = date_from.isoformat()
        if date_to is not None:
            date_filter["$lte"] = date_to.isoformat()
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        query["date"] = date_filter
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail="invalid status")
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: str = None,
    date_from: date = None,
    date_to: date = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    query = build_attendance_query(emp_code, date_from, date_to, status)
    try:
        total = db.attendance_logs.count_documents(query)
        cursor = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
        return {"items": [serialize_attendance(document) for document in cursor], "total": total, "page": page, "page_size": page_size}
    except HTTPException:
        raise
    except PyMongoError:
        logger.exception("Attendance listing failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict:
    try:
        employee = db.employees.find_one({"emp_code": body.emp_code})
        if employee is None:
            raise HTTPException(status_code=404, detail="unknown employee")
        punched_at = epoch_ms_to_datetime(body.punched_at)
        # Select the latest attendance whose punch-in is not after the supplied
        # punch-out instant, even if it is already closed (that must be a 409).
        current = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
            sort=[("punch_in", DESCENDING)],
        )
        if current is None:
            raise HTTPException(status_code=404, detail="no punch-in found")
        if current.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="attendance record is already punched out")
        punch_in_time = truncate_seconds(current["punch_in"])
        elapsed = (punched_at - punch_in_time).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
        attendance_day = parse_calendar_date(current["date"], "attendance date")
        hours = compute_work_hours(punch_in_time, punched_at)
        overtime = compute_overtime(punched_at, employee, attendance_day)
        half_day = compute_half_day(hours)
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": current["_id"],
                "status": current["status"],
                "punch_in": current["punch_in"],
                "punch_out": None,
            },
            {"$set": {"punch_out": punched_at, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="attendance record was already punched out")
        return serialize_attendance(updated)
    except HTTPException:
        raise
    except PyMongoError:
        logger.exception("Punch-out failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn) -> dict:
    target_day = date
    try:
        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(status_code=404, detail="unknown employee")
        current = db.attendance_logs.find_one({"emp_code": emp_code, "date": target_day.isoformat()})
        if current is None:
            raise HTTPException(status_code=404, detail="attendance record not found")

        requested = body.model_fields_set
        new_status = body.status if "status" in requested else current["status"]
        if new_status in ("ABSENT", "LEAVE"):
            if "punch_in" in requested or "punch_out" in requested:
                raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot be supplied with punch timestamps")
            new_punch_in = None
            new_punch_out = None
        else:
            new_punch_in = epoch_ms_to_datetime(body.punch_in) if "punch_in" in requested else current.get("punch_in")
            new_punch_out = epoch_ms_to_datetime(body.punch_out) if "punch_out" in requested else current.get("punch_out")
            if new_punch_in is None:
                raise HTTPException(status_code=422, detail="a presence status requires punch_in")
            new_punch_in = truncate_seconds(new_punch_in)
            if attendance_date_for(new_punch_in, employee) != target_day:
                raise HTTPException(status_code=422, detail="punch_in must belong to the record attendance date")
            if new_punch_out is not None:
                new_punch_out = truncate_seconds(new_punch_out)
                duration = (new_punch_out - new_punch_in).total_seconds()
                if duration <= 0 or duration > 24 * 60 * 60:
                    raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

        if new_status in ("ABSENT", "LEAVE"):
            new_punch_in = None
            new_punch_out = None
            new_work_hours = None
            new_late_minutes = 0
            new_overtime_minutes = 0
            new_half_day = False
        else:
            new_late_minutes = compute_late_minutes(new_punch_in, employee["shift_start"], target_day)
            if new_punch_out is None:
                new_work_hours = None
                new_overtime_minutes = 0
                new_half_day = False
            else:
                new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
                new_overtime_minutes = compute_overtime(new_punch_out, employee, target_day)
                new_half_day = compute_half_day(new_work_hours)

        final_values = {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        }
        old_values = {
            "status": current.get("status"),
            "punch_in": current.get("punch_in"),
            "punch_out": current.get("punch_out"),
            "work_hours": current.get("work_hours"),
            "late_minutes": current.get("late_minutes", 0),
            "overtime_minutes": current.get("overtime_minutes", 0),
            "half_day": current.get("half_day", False),
        }
        changed: dict = {}
        for field_name, new_value in final_values.items():
            old_value = old_values[field_name]
            if field_name in ("punch_in", "punch_out"):
                old_compare = truncate_seconds(old_value) if old_value is not None else None
                new_compare = truncate_seconds(new_value) if new_value is not None else None
            else:
                old_compare = old_value
                new_compare = new_value
            if old_compare != new_compare:
                changed[field_name] = {"from": old_value, "to": new_value}
        if not changed:
            raise HTTPException(status_code=422, detail="regularization does not change the record")

        history_entry = {
            "at": datetime.now(UTC).replace(microsecond=0),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changed,
        }
        # Optimistic compare-and-set: every mutable business field participates.
        # $push appends history atomically; no stale history array is replaced.
        guard: dict = {"_id": current["_id"]}
        for field_name in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
            if field_name in current:
                guard[field_name] = current[field_name]
            else:
                guard[field_name] = {"$exists": False}
        updated = db.attendance_logs.find_one_and_update(
            guard,
            {"$set": final_values, "$push": {"history": history_entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="attendance record changed concurrently; retry regularization")
        return serialize_attendance(updated)
    except HTTPException:
        raise
    except PyMongoError:
        logger.exception("Attendance regularization failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict:
    parse_month(month)
    try:
        if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="unknown employee")
        result = next(db.employees.aggregate(build_employee_monthly_pipeline(emp_code, month), allowDiskUse=True), None)
        if result is None:
            raise HTTPException(status_code=404, detail="unknown employee")
        return result
    except HTTPException:
        raise
    except PyMongoError:
        logger.exception("Employee monthly analytics failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str = None,
) -> dict:
    parse_month(month)
    try:
        items = list(db.employees.aggregate(build_department_summary_pipeline(month, department), allowDiskUse=True))
        return {"month": month, "items": items}
    except PyMongoError:
        logger.exception("Department summary failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: str = None,
) -> dict:
    parse_month(month)
    try:
        items = list(db.attendance_logs.aggregate(build_late_leaderboard_pipeline(month, limit, department), allowDiskUse=True))
        return {"month": month, "items": items}
    except PyMongoError:
        logger.exception("Late leaderboard failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
) -> dict:
    first_day = from_date
    last_day = to_date
    if last_day < first_day:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (last_day - first_day).days + 1 > 92:
        raise HTTPException(status_code=422, detail="trend range cannot exceed 92 days")
    try:
        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="unknown department")
        items = list(db.employees.aggregate(build_department_trend_pipeline(department, first_day, last_day), allowDiskUse=True))
        return {"department": department, "items": items}
    except HTTPException:
        raise
    except PyMongoError:
        logger.exception("Department trend failed")
        raise HTTPException(status_code=503, detail="Database operation failed")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str = None,
    month: str = None,
    department: str = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: date = None,
    date_to: date = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] = None,
    from_date: date = Query(default=None, alias="from"),
    to_date: date = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail=f"endpoint must be one of {sorted(allowed)}")
    try:
        if endpoint == "attendance_list":
            query = build_attendance_query(emp_code, date_from, date_to, status)
            command = {
                "find": "attendance_logs", "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size, "limit": page_size,
                "projection": {"_id": 0},
            }
            explain = _explain_collection("attendance_logs", find_command=command)
            collection_name = "attendance_logs"
        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required")
            parse_month(month)
            explain = _explain_collection("employees", build_employee_monthly_pipeline(emp_code, month))
            collection_name = "employees"
        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            parse_month(month)
            explain = _explain_collection("employees", build_department_summary_pipeline(month, department))
            collection_name = "employees"
        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            parse_month(month)
            explain = _explain_collection("attendance_logs", build_late_leaderboard_pipeline(month, limit, department))
            collection_name = "attendance_logs"
        else:
            if not department or not from_date or not to_date:
                raise HTTPException(status_code=422, detail="department, from and to are required")
            first_day = from_date
            last_day = to_date
            if last_day < first_day or (last_day - first_day).days + 1 > 92:
                raise HTTPException(status_code=422, detail="invalid department trend date range")
            explain = _explain_collection("employees", build_department_trend_pipeline(department, first_day, last_day))
            collection_name = "employees"
        return {"endpoint": endpoint, "collection": collection_name, "explain": _json_safe(explain)}
    except HTTPException:
        raise
    except PyMongoError:
        logger.exception("MongoDB explain failed")
        raise HTTPException(status_code=503, detail="Database explain failed")
