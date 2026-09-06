@echo off
REM نشر الـWorker على Cloudflare من غير أي حاجة على C: —
REM Node في E:\Projects\Tools\node · wrangler في node_modules هنا · تسجيل الدخول في E:\Projects\Tools\config
REM الاستخدام:  deploy.bat            (ينشر)
REM            deploy.bat whoami     (يتأكد من تسجيل الدخول)
REM            deploy.bat login      (لو انتهى التوكن — بيفتح المتصفح مرة واحدة)
setlocal
set "PATH=E:\Projects\Tools\node;%PATH%"
set "XDG_CONFIG_HOME=E:\Projects\Tools\config"
cd /d "%~dp0"
if "%~1"=="" (
  call "E:\Projects\Tools\node\npx.cmd" wrangler deploy
) else (
  call "E:\Projects\Tools\node\npx.cmd" wrangler %*
)
endlocal
