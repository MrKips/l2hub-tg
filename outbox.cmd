@echo off
rem Выложить наработки из outbox\ в тему «Наработки». Релизы и мост проверяет GitHub Actions.
cd /d "%~dp0"
python l2hub_tg.py outbox
