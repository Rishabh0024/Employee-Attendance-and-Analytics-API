# Architectural Design Decisions
## HROne Employee Attendance & Analytics API

## 1. Project overview
This project implements an Employee Attendance and Analytics API using Python, FastAPI, PyMongo and MongoDB. It supports employee creation/listing, attendance punch-in/punch-out, attendance regularization, monthly employee analytics, department summaries, a late-comer leaderboard, department trends and MongoDB explain output.

The assignment explicitly requires application logic to remain in `app/main.py`. The single-file design is therefore an assignment constraint, not a general recommendation for every production application.

## 2. High-Level Design (HLD)
```text
Client -> FastAPI routes -> Pydantic validation -> Business rules/helpers
       -> PyMongo -> MongoDB -> response serialization -> Client
```
FastAPI handles HTTP routing. Pydantic validates request data. Shared helpers implement date/time calculations and serialization. PyMongo performs database operations and aggregation. MongoDB stores two collections: `employees` and `attendance_logs`. API responses convert stored UTC datetimes to epoch milliseconds.

## 3. Low-Level Design (LLD)
Request models: `EmployeeIn`, `PunchInIn`, `PunchOutIn`, `RegularizeIn`.
Important helpers include `parse_calendar_date`, `parse_month`, `epoch_ms_to_datetime`, `datetime_to_epoch_ms`, `to_utc`, `truncate_seconds`, `attendance_date_for`, `compute_late_minutes`, `compute_work_hours`, `compute_overtime`, `compute_half_day`, and the serialization helpers.

The API accepts epoch milliseconds for instants. MongoDB stores UTC BSON datetimes. Calendar attendance dates and shift times use IST (UTC+05:30). Overnight shifts are assigned to the date on which the shift started. Decimal half-up rounding is used for work hours and report values.

## 4. Database design and indexes
`employees` stores employee code, name, email, department, shift and joining date. `attendance_logs` stores one employee/date attendance record, punch values, derived values and correction history.

`ensure_indexes()` declares nine indexes:
1. Unique `employees.emp_code`.
2. `employees(department, joined_on, emp_code)`.
3. `employees(department, emp_code)`.
4. `employees(joined_on, department)`.
5. Unique `attendance_logs(emp_code, date)`.
6. `attendance_logs(emp_code, punch_in DESC)`.
7. `attendance_logs(date DESC, emp_code)`.
8. `attendance_logs(status, date DESC, emp_code)`.
9. `attendance_logs(date, late_minutes, emp_code)`.

Indexes are created idempotently during startup and retried by `/health` if MongoDB was unavailable at startup. Actual index use must be checked with explain output on the target database.

## 5. Concurrency and consistency
**Punch-in:** the unique `(emp_code, date)` index is the final race-condition guard. A duplicate-key error becomes HTTP 409. This avoids relying on a check-then-insert sequence.

**Punch-out:** `find_one_and_update` matches the current record and requires `punch_out` to still be `None`. Only one competing request can close that record; a losing request receives HTTP 409.

**Regularization:** the endpoint compares current business fields before updating. It uses `$set` for corrected values and `$push` to append the audit entry in the same atomic document update. If another update wins first, the stale compare-and-set matches nothing and returns HTTP 409.

## 6. Analytics design
All report calculations are built as MongoDB aggregation pipelines rather than fetching the full attendance dataset and looping in Python.

- **Employee monthly:** `build_employee_monthly_pipeline()` starts from the employee, looks up that month's logs, counts weekdays from the later of joining date and month start, and calculates presence, leave, late and overtime totals.
- **Department summary:** `build_department_summary_pipeline()` starts from eligible employees and looks up their logs. This keeps employees with no logs in headcount. Average work hours are total hours divided by total eligible records, not an average of employee averages.
- **Late leaderboard:** `build_late_leaderboard_pipeline()` groups by employee, joins employee details, applies `$setWindowFields` with `$rank`, then applies the rank cutoff. Ties share rank; the final sort uses employee code for stable display order.
- **Department trend:** `build_department_trend_pipeline()` creates a date scaffold in MongoDB, joins daily logs, uses `$densify` and `$setWindowFields` for a seven-document moving average. The endpoint limits the range to 92 days.

## 7. Explain endpoint
`GET /admin/explain/{endpoint}` supports `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard` and `department_trend`. It asks MongoDB for `executionStats` explain output. This makes query plans inspectable; it does not prove that every query uses an index until the returned plans are checked on the target dataset.

## 8. Before and after
The starter registered five operations: health, employee create/list, punch-in and attendance list. The current code registers all 12 operations in the supplied contract.

| Area | Before | After | Numeric change |
|---|---|---|---|
| Contract operations registered | 5/12 (41.7%) | 12/12 (100%) | +58.3 percentage points |
| Application-defined indexes | 0 | 9 | 9 declared |
| Analytics/report routes | 0 | 4 reports + explain route | 5 routes added |
| Punch-out / regularization | Not implemented | Both registered and implemented | 2 added |
| Timestamp validation | Generic optional integer in starter | Strict epoch-ms integer with bounds | Validation added |
| Time handling | Naive/local datetime operations | Explicit UTC storage and IST business dates | UTC/IST boundary added |
| Punch-in race guard | Check then insert | Unique compound index | Database uniqueness guard |
| Punch-out race guard | Not implemented | Conditional atomic update | Compare-and-set |
| Correction audit | Not implemented | Atomic `$push` history | Audit append |
| Report execution | Not implemented | MongoDB aggregation pipelines | 4 report pipelines |

The percentages above describe route registration only. They do not mean that 100% of hidden tests pass. No before/after latency benchmark was supplied, so no performance percentage is claimed.

## 9. Challenges and solutions
| Challenge | Solution in current code |
|---|---|
| Duplicate simultaneous punch-ins | Unique `(emp_code, date)` index and duplicate-key handling |
| UTC/IST date confusion | UTC-aware conversion and explicit IST date calculations |
| Overnight shifts | Previous shift-start date and next-day shift-end calculation |
| Inconsistent decimal rounding | `Decimal` and `ROUND_HALF_UP` |
| Two punch-outs racing | Conditional `find_one_and_update` |
| Concurrent corrections losing audit entries | Compare-and-set plus atomic `$push` |
| Employees without logs missing from headcount | Summary starts from eligible employees |
| Incorrect average of averages | Aggregate total work hours and record count separately |
| Tied leaderboard cutoff | `$rank` before rank cutoff |
| Missing dates in trend | Database-side date scaffold, `$densify` and window stage |
| Large list query loading all rows into Python | MongoDB filter, sort, skip and limit |
| Query plans hard to inspect | `executionStats` explain endpoint |

## 10. Tests and verification
`tests/test_calculations.py` contains 13 database-free `unittest` methods covering time-rule boundaries, half-up rounding, half-day threshold, overtime, overnight shifts, epoch-millisecond truncation, request validation, leaderboard pipeline stage order, trend pipeline stages and all 12 registered API operations.

Run:
```powershell
python -m unittest discover -s tests -v
```
These tests check deterministic helpers, validation and pipeline/route structure. They do not prove live MongoDB integration, actual concurrent behaviour, Atlas compatibility, hidden-dataset correctness or performance on 100,000 records. Those require live database tests and explain-plan inspection. Record the actual test result after running the command.

## 11. Remaining verification
1. Run the unit tests and keep the real output.
2. Run the app against MongoDB 6.0+ / the assignment Atlas database.
3. Exercise concurrent punch-in, punch-out and regularization against a live database.
4. Inspect explain output for `IXSCAN`, `COLLSCAN` and execution statistics.
5. Benchmark the assignment-sized dataset before claiming performance improvements.

## 12. Trade-offs
PyMongo keeps database access straightforward. Aggregation is performed in MongoDB to avoid moving large raw datasets into Python. Compound indexes support selected query patterns but add storage and write-maintenance cost. Compare-and-set may reject a simultaneous edit with HTTP 409, which is safer than silently losing an update. The single-file application layout is retained because the assignment requires it.
