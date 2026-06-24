@echo off
REM 이 PC에서 cadpipe-run 이 잘 도는지 확인하는 테스트입니다.
REM 더블클릭만 하면 됩니다. (인터넷/파이썬/Node 설치 필요 없음)
cd /d "%~dp0"
chcp 65001 >nul
echo ============================================
echo  cadpipe-run 동작 테스트 (샘플 STEP 변환)
echo ============================================
echo.
cadpipe-run.exe "%~dp0sample_DemoBracket.step"
echo.
echo --------------------------------------------
echo  위에 [run ...] PASS 가 보이면 이 PC에서 정상 작동합니다.
echo  결과물은 이 폴더 안 cadpipe_reports 에 생겼습니다.
echo  (ERROR 가 보이면 메시지를 캡쳐해서 문의하세요.)
echo --------------------------------------------
pause
