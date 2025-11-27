@echo off
cd /d C:\Users\bs020\strategy_engine

rem 1) 최신 갱신 반영
git pull

rem 2) 올릴 파일만 명시적으로 스테이징 (허용 목록)
git add backtest/SmartFXAllocator.py
git add README.md
git add .gitignore
git add log.txt

rem 3) 커밋 (스테이징된 변경이 없으면 건너뜀)
git diff --cached --quiet
if %errorlevel%==0 (
  echo No changes to commit.
  goto PUSH
)

git commit -m "SmartFXAllocator update %date% %time%"

:PUSH
git push
echo Done.
pause
