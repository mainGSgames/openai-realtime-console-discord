@echo off
setlocal enabledelayedexpansion

:: Temporary file for storing the code files.
set temp_file=%temp%\code_files.txt
if exist "%temp_file%" del "%temp_file%"

:: Function to traverse directories and append file contents to the temp file.
for /r %%i in (*.py *.toml *.env) do (
    if "%%~nxi" neq "__pycache__" (
        echo.
        echo [%%~fi] >> "%temp_file%"
        type "%%i" >> "%temp_file%"
    )
)

:: Copy the concatenated file contents to clipboard.
type "%temp_file%" | clip

echo Code files copied to clipboard successfully!

del "%temp_file%"