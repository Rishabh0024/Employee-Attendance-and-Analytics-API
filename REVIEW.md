# REVIEW.md

I reviewed the original starter `app/main.py` and found the following problems.

The test examples below explain how to reproduce each issue in the original starter. They are not claims that all tests have already been run.

| #   | Where                       | Problem                                                                                    | How to notice it                                                | How I fixed it                                                           |
| --- | --------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 1   | `compute_late_minutes`      | It checks late minutes after removing seconds.                                             | Punch in at 09:40:01 for a 09:30 shift.                         | Check the exact 10-minute grace period before rounding down.             |
| 2   | `compute_late_minutes`      | It does not handle IST and overnight shifts correctly.                                     | Test a punch near midnight or on a different server timezone.   | Convert timestamps to IST and use the correct shift date.                |
| 3   | `compute_work_hours`        | Python `round()` does not follow half-up rounding.                                         | Test a duration that produces 9.125 hours.                      | Use `Decimal` with `ROUND_HALF_UP`.                                      |
| 4   | `compute_overtime`          | Overnight shift end is calculated on the wrong date.                                       | Test a 22:00–06:00 shift with punch-out at 06:40 next day.      | Move the shift end to the next day for overnight shifts.                 |
| 5   | `compute_overtime`          | It counts overtime even when it is less than 30 minutes.                                   | Punch out one minute after shift end.                           | Count overtime only when it reaches 30 minutes.                          |
| 6   | `GET /health`               | It does not check the MongoDB connection.                                                  | Stop MongoDB and call the endpoint.                             | Ping MongoDB and return 503 when it is unavailable.                      |
| 7   | `POST /employees`           | Duplicate check and insert are separate operations.                                        | Send two create requests for the same employee together.        | Add a unique index and return 409 on duplicate key.                      |
| 8   | `EmployeeIn` model          | Employee fields are not fully validated.                                                   | Send an invalid employee code, email, date or shift.            | Add the required Pydantic validations.                                   |
| 9   | `POST /employees`           | `created_at` uses a naive datetime and the wrong response format.                          | Check the stored value and API response.                        | Store UTC datetime and return epoch milliseconds.                        |
| 10  | `GET /employees`            | The skip calculation skips the first page.                                                 | Request page 1 with page size 20.                               | Use `(page - 1) * page_size`.                                            |
| 11  | `GET /employees`            | Total count ignores the department filter.                                                 | Filter by department and compare the total.                     | Count documents using the same filter.                                   |
| 12  | `GET /employees`            | Results are not sorted by employee code.                                                   | Insert employees in a different order and list them.            | Sort by `emp_code` ascending in MongoDB.                                 |
| 13  | `GET /employees`            | Page and page size are not validated.                                                      | Try page 0 or page size 101.                                    | Validate page and page size limits.                                      |
| 14  | `POST /attendance/punch-in` | Unknown employees are not handled safely.                                                  | Punch in using an unknown employee code.                        | Return 404 before using employee details.                                |
| 15  | `POST /attendance/punch-in` | `fromtimestamp()` uses the server's local timezone.                                        | Run on a non-IST server and test near midnight.                 | Convert epoch milliseconds to UTC and then calculate the IST date.       |
| 16  | `POST /attendance/punch-in` | Timestamp validation is missing, and zero is treated as missing.                           | Send seconds, floats or an invalid timestamp.                   | Require a valid integer epoch-millisecond timestamp.                     |
| 17  | `POST /attendance/punch-in` | Check-then-insert can create duplicate records under concurrency.                          | Send parallel punch-in requests for the same employee and date. | Use the unique `(emp_code, date)` index and handle duplicate-key errors. |
| 18  | `POST /attendance/punch-in` | Any status string is accepted.                                                             | Send `HOLIDAY` as the status.                                   | Accept only PRESENT, WFH and ON_DUTY.                                    |
| 19  | `POST /attendance/punch-in` | The response contains an extra ID field and does not follow the timestamp format.          | Compare its response with the API contract.                     | Return only the required fields and epoch-millisecond timestamps.        |
| 20  | `GET /attendance`           | All matching documents are loaded into Python.                                             | Test the endpoint with a large dataset.                         | Count, sort and paginate records in MongoDB.                             |
| 21  | `GET /attendance`           | The required employee-code tie-breaker is missing from sorting.                            | Create two records with the same date.                          | Sort by date descending and employee code ascending.                     |
| 22  | `GET /attendance`           | Date, status and pagination validation is missing.                                         | Send invalid dates, reversed dates or page size 101.            | Validate all query parameters.                                           |
| 23  | `GET /attendance`           | Internal MongoDB ID is returned as an API ID, and timestamps are not serialized correctly. | Compare response fields with the API contract.                  | Hide `_id` and convert timestamps to epoch milliseconds.                 |
| 24  | Application startup         | No required indexes are created.                                                           | Check indexes after starting on a fresh database.               | Create indexes automatically at startup.                                 |

## Things I checked and kept

- An empty `app/__init__.py` is valid.
- Using `emp_code` as the employee identifier matches the data model.
- Using `(emp_code, date)` as the attendance record key matches the data model.
- Keeping calendar dates and shift times as strings is correct.
- Pydantic `model_dump()` is correct for the required Pydantic version.
- The default employee shift and punch-in status match the assignment.

## Note

The seven endpoints marked for implementation were missing from the original starter by design. They are implemented in the submitted `app/main.py`, so I have not listed them as starter defects.
