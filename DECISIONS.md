# DECISIONS.md

1. **Indexes.** I added a unique index on `employees.emp_code` to prevent duplicate employees and a unique index on `attendance_logs(emp_code, date)` to allow only one record per employee per day. Other indexes support department filters, punch-out lookup, date sorting, status filters and monthly late records. I considered an index on `half_day`, but rejected it because it has few values and is not a main search filter.

2. **Punch-in race.** Both requests try to insert the same employee and date. The unique index allows one insert. MongoDB rejects the other with a duplicate-key error, which the API returns as 409. No check-then-insert step is used.

3. **Ties.** MongoDB `$rank` runs before the limit is applied. Employees with equal late minutes share a rank, and the next rank is skipped. All employees within the limit are returned. `emp_code` gives a stable order.

4. **Headcount.** The summary starts with employees who joined by month-end, then looks up their attendance. Employees with no logs are still counted.

5. **100x data.** I would add monthly summaries refreshed in the background. Attendance logs would remain the source of truth, and the summaries would be checked against the original records.
