from datetime import date, timedelta
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string


class Command(BaseCommand):
    help = 'Render a clearly labelled synthetic dashboard preview; reads no database records.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        output = Path(options['output'])
        if output.exists():
            raise CommandError('Preview output already exists; choose a new file.')
        today = date(2026, 9, 20)
        context = dict(preview=True, days=7, since=today - timedelta(days=6), today=today,
            enabled=True, requests=14820, errors=21, rejected=144, error_rate=0.14,
            average_ms=128, active=248, signups=42, workouts=186, tasks=412, meals=629,
            trend=[dict(day=today - timedelta(days=6-i), count=count, width=round(count/3000*100))
                   for i, count in enumerate([1200, 1680, 2010, 2140, 2490, 2300, 3000])],
            rows=[dict(endpoint=name, method=method, count=count, errors=errors, rejected=rejects, average_ms=ms)
                  for name, method, count, errors, rejects, ms in [
                      ('planner-detail', 'PATCH', 480, 9, 14, 162), ('foodmeal-list', 'POST', 860, 7, 22, 201),
                      ('workoutsession-list', 'POST', 210, 5, 8, 145), ('feed-list', 'GET', 6200, 0, 34, 98)]])
        output.write_text(render_to_string('insights/dashboard.html', context), encoding='utf-8')
        self.stdout.write(str(output))
