"""Exercise PostgreSQL authentication flows without retaining test accounts."""
import os
import secrets

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.production')
django.setup()

from django.db import connection, transaction
from rest_framework.test import APIClient

with connection.cursor() as cursor:
    cursor.execute('SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()')
    assert cursor.fetchone()[0], 'Database connection must use TLS'

with transaction.atomic():
    client = APIClient(HTTP_X_FORWARDED_PROTO='https')
    credentials = {'username': 'deploy_' + secrets.token_hex(6), 'password': secrets.token_urlsafe(32)}
    registration = dict(credentials, email=credentials['username'] + '@example.com', first_name='Deployment', last_name='Check')
    response = client.post('/api/v1/auth/register/', registration, format='json', HTTP_HOST='localhost')
    assert response.status_code == 201, (response.status_code, response.data)
    response = client.post('/api/v1/auth/login/', credentials, format='json', HTTP_HOST='localhost')
    assert response.status_code == 200, response.status_code
    client.credentials(HTTP_AUTHORIZATION='Token ' + response.data['token'])
    response = client.get('/api/v1/me/', HTTP_HOST='localhost')
    assert response.status_code == 200, response.status_code
    transaction.set_rollback(True)

print('PASS: encrypted PostgreSQL connection, registration, login, authenticated profile; test account rolled back.')
