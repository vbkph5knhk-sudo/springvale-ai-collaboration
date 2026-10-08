"""配置文件 —— 整个项目的「设置面板」都集中在这里。"""

import os

from dotenv import load_dotenv

load_dotenv()


def database_url():
    """优先读取部署平台提供的数据库地址，本地仍使用项目内 SQLite。"""
    base_dir = os.path.abspath(os.path.dirname(__file__))
    default_path = os.path.join(os.getenv('SPRINGVALE_DATA_DIR', base_dir), 'springvale.db')
    url = os.getenv('DATABASE_URL', 'sqlite:///' + default_path)
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return url


class Config:
    """项目的配置类，app.py 会读取这里的设置。"""

    BASE_DIR = os.path.abspath(os.path.dirname(__file__))

    SQLALCHEMY_DATABASE_URI = database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    APP_ENV = os.getenv('APP_ENV', 'development').lower()
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

    MAX_CONTENT_LENGTH = 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', '0') == '1'
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8

    CSRF_ENABLED = os.getenv('CSRF_ENABLED', '1') == '1'
    LOGIN_IP_MAX_ATTEMPTS = int(os.getenv('LOGIN_IP_MAX_ATTEMPTS', '10'))
    LOGIN_IP_WINDOW_SECONDS = int(os.getenv('LOGIN_IP_WINDOW_SECONDS', '60'))
    LOGIN_USER_MAX_ATTEMPTS = int(os.getenv('LOGIN_USER_MAX_ATTEMPTS', '5'))
    LOGIN_USER_WINDOW_SECONDS = int(os.getenv('LOGIN_USER_WINDOW_SECONDS', '900'))

    DEMO_PASSWORD = os.getenv('DEMO_PASSWORD', 'spring123')
    DEMO_MODE = os.getenv(
        'DEMO_MODE',
        '0' if APP_ENV == 'production' else '1',
    ) == '1'


def validate_production_config(config):
    """生产环境缺少安全配置时拒绝启动，防止带着开发密钥上线。"""
    if config.get('APP_ENV') != 'production':
        return
    secret_key = config.get('SECRET_KEY', '')
    if not secret_key or secret_key == 'dev-secret-key-change-in-production' or len(secret_key) < 32:
        raise RuntimeError('生产环境必须设置至少 32 个字符的随机 SECRET_KEY。')
    if config.get('DEMO_MODE'):
        raise RuntimeError('生产环境必须关闭 DEMO_MODE，避免公开演示账号。')


# ========== AI 助手「老镇长」配置 ==========

SENSENOVA_API_KEY = os.getenv('SENSENOVA_API_KEY', '')
SENSENOVA_BASE_URL = os.getenv('SENSENOVA_BASE_URL', 'https://api.senseaudio.cn/v1')
SENSENOVA_MODEL = os.getenv('SENSENOVA_MODEL', 'glm-5.2')
