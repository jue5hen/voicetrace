@echo off
setlocal EnableExtensions
set "ROOT=%~dp0..\"
set "PY=%ROOT%#source\engine\.venv\Scripts\python.exe"
set "ENGINE=%ROOT%#source\engine"
set "FRONTEND=%ROOT%#source\frontend"
set "TAURI=%ROOT%#source\app\src-tauri"
set "PYCACHE=%ROOT%_scratch\tests\pycache"
set "FAIL=0"
rem Python bytecode goes to _scratch\tests\pycache: no __pycache__ in the source tree (compileall has no -B).
set "PYTHONPYCACHEPREFIX=%PYCACHE%"
set "FAILED_STEPS="

echo ============================================
echo  VoiceTrace - run all tests
echo ============================================
echo  1) engine unit tests
echo  2) frontend typecheck + build
echo  3) tauri rust tests
echo  4) engine compileall (bytecode redirected, tree cleaned)
echo  5) frontend unit tests (node:test)
echo.

if not exist "%PY%" (
  echo [FATAL] engine venv python not found:
  echo         %PY%
  echo         prepare dependencies first: see README.md "Prerequisites".
  exit /b 1
)
if not exist "%TAURI%\Cargo.toml" (
  echo [FATAL] tauri project not found: %TAURI%
  exit /b 1
)

rem ---- 1/5 engine unit tests ------------------------------------------------
set "T0=%TIME%"
echo [1/5] engine unit tests
pushd "%ENGINE%"
"%PY%" -m unittest discover -s tests -v
set "RC1=%errorlevel%"
popd
set "T1=%TIME%"
call :verdict %RC1% "engine unit tests"
if not "%RC1%"=="0" call :mark_fail "engine unit tests"

rem ---- 2/5 frontend typecheck + build ---------------------------------------
set "T2=%TIME%"
echo.
echo [2/5] frontend typecheck + build
pushd "%FRONTEND%"
call pnpm build
set "RC2=%errorlevel%"
popd
set "T3=%TIME%"
call :verdict %RC2% "frontend build"
if not "%RC2%"=="0" call :mark_fail "frontend build"

rem ---- 3/5 tauri rust tests -------------------------------------------------
set "T4=%TIME%"
echo.
echo [3/5] tauri rust tests
for %%i in ("%ROOT%..\_tools\cargo\bin") do if exist "%%~fi\cargo.exe" set "PATH=%%~fi;%PATH%"
pushd "%TAURI%"
cargo test
set "RC3=%errorlevel%"
popd
set "T5=%TIME%"
call :verdict %RC3% "cargo test"
if not "%RC3%"=="0" call :mark_fail "cargo test"

rem ---- 4/5 engine compileall ------------------------------------------------
rem compileall has no -B switch (Python 3.11 only offers legacy -b), so PYTHONPYCACHEPREFIX
rem redirects bytecode under _scratch; that dir is removed afterwards and node fs.rmSync
rem clears any __pycache__ left in the source tree (workspace rule: recursive delete via node).
set "T6=%TIME%"
echo.
echo [4/5] engine compileall
pushd "%ENGINE%"
"%PY%" -m compileall -q voicetrace_engine
set "RC4A=%errorlevel%"
set "VT_ENGINE_TARGETS=%ENGINE%\voicetrace_engine;%ENGINE%\tests"
set "VT_PYCACHE=%PYCACHE%"
node -e "const fs=require('fs'),path=require('path');let n=0;const walk=(d)=>{for(const e of fs.readdirSync(d,{withFileTypes:true})){const p=path.join(d,e.name);if(e.isDirectory()){if(e.name==='__pycache__'){n++;fs.rmSync(p,{recursive:true,force:true});}else{walk(p);}}}};for(const r of (process.env.VT_ENGINE_TARGETS||'').split(';')){if(r&&fs.existsSync(r))walk(r);}const c=process.env.VT_PYCACHE;if(c)fs.rmSync(c,{recursive:true,force:true});console.log('  source tree __pycache__ removed: '+n+', temp pycache deleted');"
set "RC4B=%errorlevel%"
set "VT_ENGINE_TARGETS="
set "VT_PYCACHE="
popd
set "T7=%TIME%"
set "RC4=%RC4A%"
if not "%RC4B%"=="0" set "RC4=1"
call :verdict %RC4% "compileall"
if not "%RC4%"=="0" call :mark_fail "compileall"

rem ---- 5/5 frontend unit tests ---------------------------------------------
set "T8=%TIME%"
echo.
echo [5/5] frontend unit tests
pushd "%FRONTEND%"
call node scripts\test.mjs
set "RC5=%errorlevel%"
popd
set "T9=%TIME%"
call :verdict %RC5% "frontend tests"
if not "%RC5%"=="0" call :mark_fail "frontend tests"

rem ---- elapsed per step -----------------------------------------------------
echo.
echo ---- elapsed per step ----
powershell -NoProfile -Command "$t=@('%T0%','%T1%','%T2%','%T3%','%T4%','%T5%','%T6%','%T7%','%T8%','%T9%');$n=@('1) engine unit tests','2) frontend build','3) cargo test','4) compileall','5) frontend tests');for($i=0;$i -lt 5;$i++){try{$a=[TimeSpan]::Parse($t[2*$i].Trim());$b=[TimeSpan]::Parse($t[2*$i+1].Trim());$d=$b-$a;if($d.TotalSeconds -lt 0){$d=[TimeSpan]::FromTicks($d.Ticks+864000000000)};'{0,-22} {1,7:n1}s' -f $n[$i],$d.TotalSeconds}catch{'{0,-22} {1,7}' -f $n[$i],'n/a'}}"

echo.
if "%FAIL%"=="0" (
  echo ALL TESTS PASSED
  exit /b 0
)
echo SOME TESTS FAILED: %FAILED_STEPS%
echo see output above.
exit /b 1

rem ---- subroutines ----------------------------------------------------------
:verdict
if "%~1"=="0" (
  echo   PASS  %~2
) else (
  echo   FAIL  %~2  [exit %~1]
)
goto :eof

:mark_fail
if defined FAILED_STEPS (
  set "FAILED_STEPS=%FAILED_STEPS%; %~1"
) else (
  set "FAILED_STEPS=%~1"
)
set "FAIL=1"
goto :eof
