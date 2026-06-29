@echo off
REM One-shot builder for cadpipe-run.exe (Windows).
REM Produces dist\cadpipe-run\cadpipe-run.exe (onedir bundle).
setlocal

echo [1/4] Python deps...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :err

echo [2/4] Node deps (bundled into the exe so the target PC needs no Node)...
where npm >nul 2>nul
if errorlevel 1 (
  echo   ^(npm/Node not found - install Node.js first, or the optimize phase will be skipped^)
) else (
  REM 'call' is REQUIRED: npm is npm.cmd; without 'call' the parent batch
  REM terminates when npm finishes (window just closes mid-build).
  echo   - gltf-transform CLI ^(global; the spec bundles it into the exe^)...
  call npm install -g @gltf-transform/cli --no-fund --no-audit
  echo   - merge_faces deps ^(local core+functions^)...
  call npm install --no-fund --no-audit
)

echo [3/4] Building exe (this can take several minutes; OCP/VTK are large)...
python -m PyInstaller cadpipe-run.spec --noconfirm
if errorlevel 1 goto :err

echo [4/4] Copying convenience files next to the exe...
copy /Y "테스트하기.bat" "dist\cadpipe-run\" >nul
copy /Y "뷰어열기.bat" "dist\cadpipe-run\" >nul
copy /Y "사용법.md" "dist\cadpipe-run\" >nul
copy /Y "README.md" "dist\cadpipe-run\" >nul
copy /Y "samples\DemoBracket.step" "dist\cadpipe-run\sample_DemoBracket.step" >nul
if exist "viewer" ( mkdir "dist\cadpipe-run\viewer" 2>nul & copy /Y "viewer\*" "dist\cadpipe-run\viewer\" >nul )

echo.
echo Done.  -^>  dist\cadpipe-run\cadpipe-run.exe
echo   Test:  double-click dist\cadpipe-run\테스트하기.bat
echo   Or drag a .step file onto dist\cadpipe-run\cadpipe-run.exe
echo.
pause
goto :eof

:err
echo.
echo BUILD FAILED. See the messages above.
echo.
pause
exit /b 1
