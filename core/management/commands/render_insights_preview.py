from datetime import date, timedelta
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string

from core.analytics import _attention, _chart


class Command(BaseCommand):
    help = 'Render a clearly labelled synthetic dashboard preview; reads no database records.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        output = Path(options['output'])
        if output.exists():
            raise CommandError('Preview output already exists; choose a new file.')
        today = date(2026, 9, 20)
        days = 7
        since = today - timedelta(days=days - 1)
        slow_ms = 3000
        # Built through the same helpers the view uses, so the preview cannot
        # quietly show a layout the real page can no longer produce.
        trend = [
            dict(day=since + timedelta(days=index), count=count, errors=errors, people=people)
            for index, (count, errors, people) in enumerate([
                (1200, 0, 96), (1680, 2, 121), (2010, 0, 148), (2140, 9, 166),
                (2490, 1, 190), (2300, 0, 204), (3000, 9, 248),
            ])
        ]
        peak = max(row['count'] for row in trend)
        for row in trend:
            row['width'] = round(row['count'] / peak * 100)
        rows = [
            dict(endpoint=name, method=method, count=count, errors=errors,
                 rejected=rejects, average_ms=ms, max_ms=worst)
            for name, method, count, errors, rejects, ms, worst in [
                ('planner-detail', 'PATCH', 480, 9, 14, 162, 4120),
                ('foodmeal-list', 'POST', 860, 7, 22, 201, 2980),
                ('workoutsession-list', 'POST', 210, 5, 8, 145, 1100),
                ('food-search', 'GET', 1340, 0, 61, 402, 9240),
                ('feed-list', 'GET', 6200, 0, 34, 98, 760),
            ]
        ]
        context = dict(
            preview=True, days=days, since=since, today=today,
            prior_start=since - timedelta(days=days), prior_end=since - timedelta(days=1),
            enabled=True, slow_ms=slow_ms,
            requests=14820, errors=21, rejected=144, error_rate=0.14, average_ms=128,
            active=248, signups=42, workouts=186, tasks=412, meals=629,
            deltas={
                'requests': {'text': '+18% vs previous 7 days', 'tone': 'good'},
                'active': {'text': '+11% vs previous 7 days', 'tone': 'good'},
                'workouts': {'text': '-6% vs previous 7 days', 'tone': 'bad'},
                'error_rate': {'text': '+40% vs previous 7 days', 'tone': 'bad'},
                'average_ms': {'text': '-9% vs previous 7 days', 'tone': 'good'},
            },
            returning=171, lapsed=52, new_to_api=77, return_rate=77,
            activated=17, activation_rate=40,
            attention=_attention(rows, slow_ms), chart=_chart(trend),
            rows=rows, trend=trend,
        )
        output.write_text(render_to_string('insights/dashboard.html', context), encoding='utf-8')
        self.stdout.write(str(output))
