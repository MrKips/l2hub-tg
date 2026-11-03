@echo off
rem Запуск бота группы L2Hub для Планировщика Windows (раз в 10 минут).
cd /d "%~dp0"
python l2hub_tg.py watch >> stateun.log 2>&1
