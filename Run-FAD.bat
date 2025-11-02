@echo off
setlocal

REM === Paths ===
set "SRC=R:\PGS\PC\00 Projects\202110 Gabon BW Energy\fads"
set "DST=\\wsl.localhost\Ubuntu-24.04\home\hyppc\repo-fad"
set "OUT=\\wsl.localhost\Ubuntu-24.04\home\hyppc\repo-fad\fad_tracks_output"

REM === Ensure destination exists ===
if not exist "%DST%" mkdir "%DST%"

echo Copying new buoy CSV files from Windows to WSL...
REM /XO = skip older, /R:1 /W:1 = retry fast, exit codes 0..7 are success/info
robocopy "%SRC%" "%DST%" "buoys*.csv" /XO /R:1 /W:1 /NFL /NDL /NP /NJH /NJS
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo Robocopy failed with code %RC%.
  goto :END
)

echo Cleaning up IDENTIFIER files...
del /q "%DST%\*IDENTIFIER*" >nul 2>&1
del /q "%DST%\*Zone.Identifier*" >nul 2>&1

echo Running build_fad_tracks.py inside WSL (Conda base)...
wsl.exe -d Ubuntu-24.04 bash -lc "source ~/.miniforge3/etc/profile.d/conda.sh; conda activate base; cd /home/hyppc/repo-fad && python build_fad_tracks.py"
set "WC=%ERRORLEVEL%"

if exist "%OUT%" (
  echo Opening output folder...
  start "" explorer.exe "%OUT%"
)

if %WC% NEQ 0 (
  echo The build reported an error (code %WC%).
)

:END
echo.
pause
endlocal