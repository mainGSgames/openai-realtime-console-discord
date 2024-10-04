@echo off
setlocal EnableDelayedExpansion

:: Define the temporary files to store file list and content
echo Generating project summary...
set fileList=%temp%\project_file_list.txt
set fileContent=%temp%\project_file_content.txt
set combinedContent=%temp%\project_combined.txt

:: Clear previous temp files (if any)
if exist !fileList! del /f /q !fileList!
if exist !fileContent! del /f /q !fileContent!
if exist !combinedContent! del /f /q !combinedContent!

:: Collect file structure and file paths, excluding node_modules, package-lock.json, .git, and this script
for /f "delims=" %%f in ('dir /s /b /a-d ^| findstr /i /v "node_modules package-lock.json .git copy_project_to_clipboard.bat"') do (
    echo %%f >> !fileList!
    echo %%f | findstr /i ".png .svg .txt" >nul || (
        echo File: %%f >> !fileContent!
        type "%%f" >> !fileContent!
        echo( >> !fileContent!
    )
)

:: Combine the file structure and file contents
(echo FILE STRUCTURE:) > !combinedContent!
for /f "delims=\ tokens=*" %%a in (!fileList!) do (
    set "line=%%a"
    set "line=!line:%cd%=.!"
    echo !line! >> !combinedContent!
)
echo. >> !combinedContent!
(echo CONTENTS:) >> !combinedContent!
type !fileContent! >> !combinedContent!

:: Copy the combined content to clipboard using PowerShell
powershell -command "Get-Content -Path '!combinedContent!' | Set-Clipboard"

:: Cleanup temporary files
del /f /q !fileList!
del /f /q !fileContent!
del /f /q !combinedContent!

echo Project files and content copied to clipboard.
pause