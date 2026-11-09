"""What one request cost the database, reported on the response.

Only `config.loadtest` installs this. It is outermost so the work
AnalyticsMiddleware does after the view is counted with everything else.

`execute_wrapper` sees statements and savepoints. It does not see BEGIN and
COMMIT, which do not go through a cursor, so the count is the same measure
before and after a change but is not a count of network round trips.
"""

import re
import time

from django.db import connection

METRIC_TABLES = re.compile(r"core_(dailyapimetric|recentapimetric|dailyactiveaccount)")


class _Cost:
    def __init__(self):
        self.statements = 0
        self.seconds = 0.0
        self.metric_statements = 0

    def __call__(self, execute, sql, params, many, context):
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.seconds += time.perf_counter() - started
            self.statements += 1
            if METRIC_TABLES.search(sql):
                self.metric_statements += 1


class RequestCostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        cost = _Cost()
        with connection.execute_wrapper(cost):
            response = self.get_response(request)
        response["X-Loadtest-Sql-Count"] = str(cost.statements)
        response["X-Loadtest-Sql-Ms"] = f"{cost.seconds * 1000:.2f}"
        response["X-Loadtest-Metric-Sql"] = str(cost.metric_statements)
        return response
