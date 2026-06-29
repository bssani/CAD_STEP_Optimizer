@echo off
REM 오프라인 GLB 뷰어를 기본 브라우저로 엽니다. (인터넷/업로드 없음 - 데이터는 PC 밖으로 안 나갑니다)
REM 열린 화면에서 production.glb 를 끌어다 놓거나 'GLB 열기'로 선택하세요.
start "" "%~dp0viewer\viewer.html"
