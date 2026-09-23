# This file is part of wger Workout Manager under the GNU AGPL v3 or later.
# Production settings for the pinned Cubatica/wger release.

import hashlib
import ipaddress
import os
import secrets
import warnings

import environ

from .settings_global import *  # noqa: F403
# One athlete-facing locale covers templates, forms and API-rendered dates.
LANGUAGE_CODE = 'en-au'
LANGUAGES = (('en-au', 'Australian English'),)
AVAILABLE_LANGUAGES = LANGUAGES
FORMAT_MODULE_PATH = 'wger.formats'



env = environ.Env(DJANGO_DEBUG=(bool, False))

_DEFAULT_KEYS = {'wger-docker-supersecret-key-1234567890!@#$%^&*(-_)', 'wger-docker-secret-jwtkey-1234567890!@#$%^&*(-_=+)'}
_DEFAULT_JWT_KEY_HASHES = {'993583b530ed307419c1fd54eef8c7010ac29fa9ee6ddfa3ed0e5204e2b5e99a', '964a4ebdb0d1b9f7ac461711a8861d74bd887a955b0e10d79b93c532f1e13d98'}
DEBUG = env('DJANGO_DEBUG')

if os.environ.get('DJANGO_ADMINS'):
    ADMINS = [env.tuple('DJANGO_ADMINS')]
    MANAGERS = ADMINS

if os.environ.get('PS_DATABASE_URI'):
    DATABASES = {'default': env.db_url('PS_DATABASE_URI')}
else:
    DATABASES = {'default': {'ENGINE': env.str('DJANGO_DB_ENGINE'), 'NAME': env.str('DJANGO_DB_DATABASE'), 'USER': env.str('DJANGO_DB_USER', ''), 'PASSWORD': env.str('DJANGO_DB_PASSWORD', ''), 'HOST': env.str('DJANGO_DB_HOST', ''), 'PORT': env.int('DJANGO_DB_PORT', 5432)}}
PS_STORAGE_PG_URI = env.str('PS_STORAGE_PG_URI', 'postgres://powersync_storage:powersync_password@db:5432/wger')
TIME_ZONE = env.str('TIME_ZONE', 'Europe/Berlin')
SECRET_KEY = env.str('SECRET_KEY', '')
if not SECRET_KEY or (SECRET_KEY in _DEFAULT_KEYS and not DEBUG):
    SECRET_KEY = secrets.token_urlsafe(50)
    if not DEBUG:
        warnings.warn('SECRET_KEY is not set or uses the default value so a random key was generated, sessions will not persist across restarts. Set SECRET_KEY in your environment for production use.', stacklevel=1)

JWT_PUBLIC_KEY = env.str('JWT_PUBLIC_KEY', '')
JWT_PRIVATE_KEY = env.str('JWT_PRIVATE_KEY', '')
POWERSYNC_URL_PATH = env.str('POWERSYNC_URL_PATH', 'ps')
POWERSYNC_URL = env.str('POWERSYNC_URL', '')
POWERSYNC_TOKEN_LIFETIME = env.int('POWERSYNC_TOKEN_LIFETIME', 600)
if not DEBUG and any(key and hashlib.sha256(key.encode()).hexdigest() in _DEFAULT_JWT_KEY_HASHES for key in (JWT_PUBLIC_KEY, JWT_PRIVATE_KEY)):
    warnings.warn('JWT_PUBLIC_KEY / JWT_PRIVATE_KEY use shipped defaults', stacklevel=1)
if not DEBUG and not (JWT_PRIVATE_KEY and JWT_PUBLIC_KEY):
    warnings.warn('JWT authentication is not configured', stacklevel=1)
IDP_OIDC_PRIVATE_KEY = env.str('IDP_OIDC_PRIVATE_KEY', '', multiline=True)
RECAPTCHA_PUBLIC_KEY = env.str('RECAPTCHA_PUBLIC_KEY', '')
RECAPTCHA_PRIVATE_KEY = env.str('RECAPTCHA_PRIVATE_KEY', '')
RECAPTCHA_REQUIRED_SCORE = env.float('RECAPTCHA_REQUIRED_SCORE', 0.75)
SITE_URL = env.str('SITE_URL', 'http://localhost:8000')
MEDIA_ROOT = env.str('DJANGO_MEDIA_ROOT', '/home/wger/media')
STATIC_ROOT = env.str('DJANGO_STATIC_ROOT', '/home/wger/static')
MEDIA_URL = env.str('MEDIA_URL', '/media/')
STATIC_URL = env.str('STATIC_URL', '/static/')
LOGIN_REDIRECT_URL = env.str('LOGIN_REDIRECT_URL', '/')
ALLOWED_HOSTS = ['*']
SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'

if DEBUG:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
if env.bool('ENABLE_EMAIL', False):
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = env.str('EMAIL_HOST')
    EMAIL_PORT = env.int('EMAIL_PORT')
    EMAIL_HOST_USER = env.str('EMAIL_HOST_USER')
    EMAIL_HOST_PASSWORD = env.str('EMAIL_HOST_PASSWORD')
    EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', True)
    EMAIL_USE_SSL = env.bool('EMAIL_USE_SSL', False)
    EMAIL_TIMEOUT = 60
DEFAULT_FROM_EMAIL = env.str('FROM_EMAIL', 'wger Workout Manager <wger@example.com>')
WGER_SETTINGS['EMAIL_FROM'] = DEFAULT_FROM_EMAIL
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_FROM_ADDRESS = DEFAULT_FROM_EMAIL

WGER_SETTINGS['ALLOW_GUEST_USERS'] = env.bool('ALLOW_GUEST_USERS', True)
WGER_SETTINGS['ALLOW_REGISTRATION'] = env.bool('ALLOW_REGISTRATION', True)
WGER_SETTINGS['ALLOW_UPLOAD_VIDEOS'] = env.bool('ALLOW_UPLOAD_VIDEOS', True)
WGER_SETTINGS['DOWNLOAD_INGREDIENTS_FROM'] = env.str('DOWNLOAD_INGREDIENTS_FROM', 'WGER')
WGER_SETTINGS['EXERCISE_CACHE_TTL'] = env.int('EXERCISE_CACHE_TTL', 604800)
WGER_SETTINGS['MIN_ACCOUNT_AGE_TO_TRUST'] = env.int('MIN_ACCOUNT_AGE_TO_TRUST', 21)
for key in ('SYNC_EXERCISES_CELERY', 'SYNC_EXERCISE_IMAGES_CELERY', 'SYNC_EXERCISE_VIDEOS_CELERY', 'SYNC_INGREDIENTS_CELERY', 'SYNC_OFF_DAILY_DELTA_CELERY', 'EXPORT_INGREDIENTS_BULK_CELERY', 'USE_RECAPTCHA', 'USE_CELERY', 'CACHE_API_EXERCISES_CELERY', 'CACHE_API_EXERCISES_CELERY_FORCE_UPDATE'):
    WGER_SETTINGS[key] = env.bool(key, False)
WGER_SETTINGS['SYNC_INGREDIENTS_DUMP_URL'] = env.str('SYNC_INGREDIENTS_DUMP_URL', 'https://wger.de/media/ingredients/ingredients.jsonl.gz')
EMAIL_DELIVERY_BACKEND = EMAIL_BACKEND
if WGER_SETTINGS['USE_CELERY']:
    EMAIL_BACKEND = 'wger.core.mail.CeleryEmailBackend'
WGER_SHOW_APP_STORE_LINKS = env.bool('WGER_SHOW_APP_STORE_LINKS', True)
WGER_MAX_SESSION_LENGTH_HOURS = env.int('WGER_MAX_SESSION_LENGTH_HOURS', 5)
AUTH_PROXY_HEADER = env.str('AUTH_PROXY_HEADER', '')
AUTH_PROXY_TRUSTED_IPS = env.list('AUTH_PROXY_TRUSTED_IPS', default=[])
AUTH_PROXY_CREATE_UNKNOWN_USER = env.bool('AUTH_PROXY_CREATE_UNKNOWN_USER', False)
AUTH_PROXY_USER_EMAIL_HEADER = env.str('AUTH_PROXY_USER_EMAIL_HEADER', '')
AUTH_PROXY_USER_NAME_HEADER = env.str('AUTH_PROXY_USER_NAME_HEADER', '')

if os.environ.get('DJANGO_CACHE_BACKEND'):
    CACHES = {'default': {'BACKEND': env.str('DJANGO_CACHE_BACKEND'), 'LOCATION': env.str('DJANGO_CACHE_LOCATION', ''), 'TIMEOUT': env.int('DJANGO_CACHE_TIMEOUT', 300), 'OPTIONS': {'CLIENT_CLASS': env.str('DJANGO_CACHE_CLIENT_CLASS', '')}}}
    if os.environ.get('DJANGO_CACHE_CLIENT_PASSWORD'):
        CACHES['default']['OPTIONS']['PASSWORD'] = env.str('DJANGO_CACHE_CLIENT_PASSWORD')

MFA_SUPPORTED_TYPES = env.list('MFA_SUPPORTED_TYPES', default=['totp', 'recovery_codes', 'webauthn'])
AXES_ENABLED = env.bool('AXES_ENABLED', True)
AXES_LOCKOUT_PARAMETERS = env.list('AXES_LOCKOUT_PARAMETERS', default=['ip_address'])
AXES_FAILURE_LIMIT = env.int('AXES_FAILURE_LIMIT', 10)
AXES_COOLOFF_TIME = timedelta(minutes=env.float('AXES_COOLOFF_TIME', 30))
AXES_HANDLER = env.str('AXES_HANDLER', 'axes.handlers.cache.AxesCacheHandler')
AXES_IPWARE_PROXY_COUNT = env.int('AXES_IPWARE_PROXY_COUNT', 0)
AXES_IPWARE_META_PRECEDENCE_ORDER = env.list('AXES_IPWARE_META_PRECEDENCE_ORDER', default=['REMOTE_ADDR'])
AXES_NEVER_LOCKOUT_WHITELIST = env.bool('AXES_NEVER_LOCKOUT_WHITELIST', True)
AXES_IP_WHITELIST = env.list('AXES_IP_WHITELIST', default=[])
_OUR_NETWORKS = tuple(ipaddress.ip_network(network) for network in ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '::1/128'))

def _never_lock_our_nets(request, credentials=None):
    candidates = (request.META.get('REMOTE_ADDR', ''), request.META.get('HTTP_X_FORWARDED_FOR', '').split(',', 1)[0].strip())
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if any(address in network for network in _OUR_NETWORKS):
            return True
    return False

AXES_WHITELIST_CALLABLE = _never_lock_our_nets

WGER_SOCIAL_PROVIDERS = env.list('WGER_SOCIAL_PROVIDERS', default=[])
if WGER_SOCIAL_PROVIDERS:
    INSTALLED_APPS += [f'allauth.socialaccount.providers.{provider}' for provider in WGER_SOCIAL_PROVIDERS]
REFRESH_TOKEN_LIFETIME_HOURS = env.int('REFRESH_TOKEN_LIFETIME', 24 * 30 * 4)
SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'] = timedelta(minutes=env.int('ACCESS_TOKEN_LIFETIME', 15))
SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'] = timedelta(hours=REFRESH_TOKEN_LIFETIME_HOURS)
_jwt_private_pem = jwk_b64_to_pem(JWT_PRIVATE_KEY)
_jwt_public_pem = jwk_b64_to_pem(JWT_PUBLIC_KEY)
SIMPLE_JWT['SIGNING_KEY'] = _jwt_private_pem
SIMPLE_JWT['VERIFYING_KEY'] = _jwt_public_pem
HEADLESS_JWT_PRIVATE_KEY = _jwt_private_pem
HEADLESS_JWT_REFRESH_TOKEN_EXPIRES_IN = REFRESH_TOKEN_LIFETIME_HOURS * 3600
CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=['http://127.0.0.1', 'http://localhost', 'https://localhost'])
if env.bool('X_FORWARDED_PROTO_HEADER_SET', False):
    SECURE_PROXY_SSL_HEADER = (env.str('SECURE_PROXY_SSL_HEADER', 'HTTP_X_FORWARDED_PROTO'), 'https')
USE_X_FORWARDED_HOST = env.bool('USE_X_FORWARDED_HOST', False)
REST_FRAMEWORK['NUM_PROXIES'] = env.int('NUMBER_OF_PROXIES', 1)
CELERY_BROKER_URL = env.str('CELERY_BROKER', 'redis://cache:6379/2')
CELERY_RESULT_BACKEND = env.str('CELERY_BACKEND', 'redis://cache:6379/2')
EXPOSE_PROMETHEUS_METRICS = env.bool('EXPOSE_PROMETHEUS_METRICS', False)
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'formatters': {'simple': {'format': 'level={levelname} ts={asctime} module={module} path={pathname} line={lineno} message={message}', 'style': '{'}}, 'handlers': {'console': {'level': 'DEBUG', 'class': 'logging.StreamHandler', 'formatter': 'simple'}}, 'loggers': {'': {'handlers': ['console'], 'level': env.str('LOG_LEVEL_PYTHON', 'INFO').upper(), 'propagate': True}}}
STORAGES = {'default': {'BACKEND': env.str('DJANGO_STORAGES_DEFAULT_BACKEND', 'django.core.files.storage.FileSystemStorage')}, 'staticfiles': {'BACKEND': env.str('DJANGO_STORAGES_STATICFILES_BACKEND', 'wger.core.storage.LenientManifestStaticFilesStorage')}}
USE_S3_MEDIA_FILES = env.bool('USE_S3_MEDIA_FILES', False)
USE_S3_STATIC_FILES = env.bool('USE_S3_STATIC_FILES', False)
if USE_S3_MEDIA_FILES or USE_S3_STATIC_FILES:
    AWS_ACCESS_KEY_ID = env.str('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = env.str('AWS_SECRET_ACCESS_KEY')
    AWS_STORAGE_BUCKET_NAME = env.str('AWS_STORAGE_BUCKET_NAME')
    AWS_S3_REGION_NAME = env.str('AWS_S3_REGION_NAME')
    AWS_S3_DOMAIN = env.str('AWS_S3_DOMAIN')
    AWS_S3_ENDPOINT_URL = env.str('AWS_S3_ENDPOINT_URL', f'https://{AWS_S3_REGION_NAME}.{AWS_S3_DOMAIN}')
    AWS_S3_CUSTOM_DOMAIN = env.str('AWS_S3_CUSTOM_DOMAIN', f'{AWS_STORAGE_BUCKET_NAME}.{AWS_S3_REGION_NAME}.{AWS_S3_DOMAIN}')
    AWS_QUERYSTRING_AUTH = False
    if USE_S3_MEDIA_FILES:
        STORAGES['default'] = {'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage', 'OPTIONS': {'location': env.str('S3_MEDIA_FILES_LOCATION', 'media')}}
        if env.bool('USE_S3_URL_FOR_MEDIA', True):
            MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/'
    if USE_S3_STATIC_FILES:
        STORAGES['staticfiles'] = {'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage', 'OPTIONS': {'location': env.str('S3_STATIC_FILES_LOCATION', 'static')}}
        if env.bool('USE_S3_URL_FOR_STATIC', True):
            STATIC_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/'
