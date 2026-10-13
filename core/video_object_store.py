"""Private S3-compatible objects; API traffic carries only upload policies."""
from django.conf import settings


def client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=settings.VIDEO_STORAGE_ENDPOINT or None,
        region_name=settings.VIDEO_STORAGE_REGION,
        aws_access_key_id=settings.VIDEO_STORAGE_ACCESS_KEY or None,
        aws_secret_access_key=settings.VIDEO_STORAGE_SECRET_KEY or None,
        config=Config(signature_version='s3v4', connect_timeout=5, read_timeout=30,
            retries={'max_attempts': 2, 'mode': 'standard'}))


def upload_policy(key, size, content_type):
    return client().generate_presigned_post(Bucket=settings.VIDEO_STORAGE_BUCKET, Key=key,
        Fields={'Content-Type': content_type},
        Conditions=[['content-length-range', size, size], {'Content-Type': content_type}], ExpiresIn=300)


def head(key):
    return client().head_object(Bucket=settings.VIDEO_STORAGE_BUCKET, Key=key)


def download(key, etag, path, limit):
    response = client().get_object(Bucket=settings.VIDEO_STORAGE_BUCKET, Key=key, IfMatch=etag)
    body = response['Body']
    total = 0
    try:
        with open(path, 'wb') as destination:
            while chunk := body.read(256 * 1024):
                total += len(chunk)
                if total > limit:
                    raise ValueError('oversized_source')
                destination.write(chunk)
    finally:
        body.close()
    return total


def upload(key, path, content_type):
    from boto3.s3.transfer import TransferConfig
    client().upload_file(str(path), settings.VIDEO_STORAGE_BUCKET, key,
        ExtraArgs={'ContentType': content_type, 'CacheControl': 'private, max-age=30'},
        Config=TransferConfig(max_concurrency=1, use_threads=False))


def playback(key):
    return client().generate_presigned_url('get_object',
        Params={'Bucket': settings.VIDEO_STORAGE_BUCKET, 'Key': key}, ExpiresIn=60)


def delete(key):
    client().delete_object(Bucket=settings.VIDEO_STORAGE_BUCKET, Key=key)
