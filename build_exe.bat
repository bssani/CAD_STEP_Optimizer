@echo off
REM One-shot builder for cadpipe-run.exe (Windows).
REM Produces dist\cadpipe-run\cadpipe-run.exe (onedir bundle).
setlocal

echo [1/4] Python deps...
python -m pip install -r requirements.txt pyinstaller || goto :err

echo [2/4] Node deps (for the optimize phase's in-part face merge)...
where npm >nul 2>nul && npm install --no-fund --no-audit
where npm >nul 2>nul || echo   (npm not found - optimize phase will be skipped at runtime)

echo [3/4] Building exe (this can take several minutes; OCP/VTK are large)...
python -m PyInstaller cadpipe-run.spec --noconfirm || goto :err

echo [4/4] Copying convenience files next to the exe...
copy /Y "테스트하기.bat" "dist\cadpipe-run\" >nul
copy /Y "사용법.md" "dist\cadpipe-run\" >nul
copy /Y "README.md" "dist\cadpipe-run\" >nul
copy /Y "samples\DemoBracket.step" "dist\cadpipe-run\sample_DemoBracket.step" >nul

echo Done.
echo   Test on this/any PC:  double-click dist\cadpipe-run\테스트하기.bat
echo   Or drag a .step file onto dist\cadpipe-run\cadpipe-run.exe
goto :eof

:err
echo BUILD FAILED. See output above.
exit /b 1
