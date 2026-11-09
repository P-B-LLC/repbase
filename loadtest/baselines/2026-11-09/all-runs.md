| Run | Requests/s | 5xx+fail % | First page p95 | Later page p95 | Workout p95 | Planner p95 | Food p95 |
|---|---|---|---|---|---|---|---|
| before-core-only | 12.75 | 0.0 | None | None | 32.0 | 27.3 | 37.6 |
| before-mixed-warm | 26.15 | 0.0 | 265.2 | 127.9 | 34.0 | 30.1 | 30.1 |
| before-mixed-warm-repeat | 26.13 | 0.0 | 271.0 | 128.6 | 53.6 | 27.6 | 29.7 |
| before-mixed-cold | 26.13 | 0.0 | 257.6 | 137.2 | 111.0 | 25.8 | 37.1 |
| before-mixed-2x | 50.19 | 0.0 | 291.9 | 164.7 | 28.5 | 29.0 | 30.3 |
| before-stats-late-cold | 26.12 | 0.0 | 273.8 | 126.9 | 34.3 | 26.9 | 30.9 |
| before-stats-missing | 26.13 | 0.0 | 259.2 | 133.4 | 154.4 | 26.0 | 36.5 |
| before-redis-blackhole | 2.92 | 100.0 | 30003.6 | None | 30003.7 | 30003.7 | 30003.9 |
| after-core-only | 12.73 | 0.0 | None | None | 29.2 | 24.6 | 36.7 |
| after-mixed-warm | 26.14 | 0.0 | 255.7 | 116.9 | 73.0 | 22.6 | 29.9 |
| after-mixed-warm-repeat | 26.15 | 0.0 | 238.4 | 120.1 | 46.8 | 23.3 | 32.6 |
| after-mixed-cold | 26.15 | 0.0 | 243.4 | 118.3 | 37.0 | 24.2 | 31.8 |
| after-mixed-2x | 50.23 | 0.0 | 282.6 | 157.7 | 21.9 | 22.4 | 23.2 |
| after-stats-late-cold | 26.14 | 0.0 | 260.1 | 115.1 | 25.7 | 24.4 | 28.5 |
| after-stats-missing | 26.13 | 0.0 | 261.9 | 128.5 | 29.9 | 23.8 | 33.1 |
| after-redis-blackhole | 15.37 | 11.497 | 9.5 | None | 124.8 | 132.7 | 91.5 |
| after-redis-refused | 15.35 | 11.509 | 9.4 | None | 116.0 | 77.4 | 70.6 |
| after-redis-up-control | 27.68 | 0.0 | 243.6 | 111.2 | 658.2 | 256.6 | 335.5 |

| Run | SQL statements/request | Metric SQL/request | Rows examined/request | DB CPU cores | Peak connections | Lock waiters peak | Cache hit % | Stats age s | Flush lag s |
|---|---|---|---|---|---|---|---|---|---|
| before-core-only | 11.75 | 5.0 | 2874.5 | 0.008 | 8/50 | 0 | 69.67 | 126.9 | None |
| before-mixed-warm | 12.94 | 5.004 | 29223.0 | 0.084 | 8/50 | 0 | 96.83 | 127.0 | None |
| before-mixed-warm-repeat | 12.93 | 5.0 | 29996.8 | 0.395 | 9/50 | 1 | 96.86 | 119.3 | None |
| before-mixed-cold | 12.95 | 5.005 | 30356.2 | 0.396 | 11/50 | 2 | 94.55 | 103.6 | None |
| before-mixed-2x | 12.98 | 5.0 | 28551.4 | 0.657 | 9/50 | 1 | 98.15 | 118.7 | None |
| before-stats-late-cold | 12.99 | 5.004 | 31709.6 | 0.378 | 8/50 | 0 | 95.47 | 7374.6 | None |
| before-stats-missing | 12.97 | 5.0 | 30089.0 | 0.386 | 8/50 | 2 | 97.94 | None | None |
| before-redis-blackhole | 1.46 | 1.086 | 342.7 | 0.005 | 9/50 | 0 | None | 150.8 | None |
| after-core-only | 6.75 | 0.0 | 2112.9 | 0.02 | 8/50 | 0 | 97.08 | 228.6 | 25.9 |
| after-mixed-warm | 7.93 | 0.0 | 29181.1 | 0.384 | 8/50 | 0 | 97.04 | 119.3 | 24.0 |
| after-mixed-warm-repeat | 7.93 | 0.0 | 29009.4 | 0.37 | 8/50 | 0 | 97.04 | 119.3 | 24.1 |
| after-mixed-cold | 7.93 | 0.0 | 29024.1 | 0.352 | 8/50 | 0 | 94.53 | 103.7 | 24.0 |
| after-mixed-2x | 7.98 | 0.0 | 29837.6 | 0.652 | 8/50 | 0 | 98.16 | 118.8 | 23.5 |
| after-stats-late-cold | 7.93 | 0.0 | 29050.7 | 0.361 | 8/50 | 0 | 50.93 | 7375.2 | 24.0 |
| after-stats-missing | 8.05 | 0.0 | 30154.7 | 0.369 | 8/50 | 0 | 97.73 | None | 23.9 |
| after-redis-blackhole | 6.15 | 0.0 | 5915.7 | 0.039 | 7/50 | 0 | None | 88.7 | None |
| after-redis-refused | 6.16 | 0.0 | 5922.2 | 0.037 | 5/50 | 0 | None | 88.5 | None |
| after-redis-up-control | 7.61 | 0.0 | 41532.1 | 0.422 | 8/50 | 0 | 90.05 | 43.5 | 21.8 |
