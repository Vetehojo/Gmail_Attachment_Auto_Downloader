"""Task Scheduler read-back XML captured on a real machine (owner-approved E2E, 2026-09-24).

Bytes are exactly what `schtasks /Query /TN <name> /XML` wrote to a pipe on
Windows 11 ja-JP: no BOM, an encoding="UTF-16" declaration over code-page
bytes, and CR CR LF line endings. Sanitized for the public repo: the account
SID and user name are synthetic, and the E2E dummy-script argument (a scratch
path plus a distinguishing word) is replaced by this app's single quoted
script path. Nothing else was changed.

LEGACY_* : created by the previous flag-based registration
           (/TR '"pythonw" "script"' /RU user /IT /RL LIMITED + schedule flags).
XML_*    : created by /Create /XML with explicit settings, then read back.
"""

FIXTURE_SID = "S-1-5-21-1000-2000-3000-1001"
FIXTURE_USER = r"PC\user"
FIXTURE_PYTHONW = r"C:\Python314\pythonw.exe"
FIXTURE_APP_SCRIPT = r"C:\App\app\gmail_app.py"
FIXTURE_WATCHDOG_SCRIPT = r"C:\App\app\watchdog.py"

LEGACY_WATCHDOG = (
    b'<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
    b'<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\r\n'
    b'  <RegistrationInfo>\r\r\n'
    b'    <Date>2026-09-24T00:51:54</Date>\r\r\n'
    b'    <Author>PC\\user</Author>\r\r\n'
    b'    <URI>\\GAD E2E Test Watchdog</URI>\r\r\n'
    b'  </RegistrationInfo>\r\r\n'
    b'  <Principals>\r\r\n'
    b'    <Principal id="Author">\r\r\n'
    b'      <UserId>S-1-5-21-1000-2000-3000-1001</UserId>\r\r\n'
    b'      <LogonType>InteractiveToken</LogonType>\r\r\n'
    b'    </Principal>\r\r\n'
    b'  </Principals>\r\r\n'
    b'  <Settings>\r\r\n'
    b'    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>\r\r\n'
    b'    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>\r\r\n'
    b'    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\r\n'
    b'    <IdleSettings>\r\r\n'
    b'      <Duration>PT10M</Duration>\r\r\n'
    b'      <WaitTimeout>PT1H</WaitTimeout>\r\r\n'
    b'      <StopOnIdleEnd>true</StopOnIdleEnd>\r\r\n'
    b'      <RestartOnIdle>false</RestartOnIdle>\r\r\n'
    b'    </IdleSettings>\r\r\n'
    b'  </Settings>\r\r\n'
    b'  <Triggers>\r\r\n'
    b'    <TimeTrigger>\r\r\n'
    b'      <StartBoundary>2026-09-24T00:51:00</StartBoundary>\r\r\n'
    b'      <Repetition>\r\r\n'
    b'        <Interval>PT5M</Interval>\r\r\n'
    b'      </Repetition>\r\r\n'
    b'    </TimeTrigger>\r\r\n'
    b'  </Triggers>\r\r\n'
    b'  <Actions Context="Author">\r\r\n'
    b'    <Exec>\r\r\n'
    b'      <Command>"C:\\Python314\\pythonw.exe"</Command>\r\r\n'
    b'      <Arguments>"C:\\App\\app\\watchdog.py"</Arguments>\r\r\n'
    b'    </Exec>\r\r\n'
    b'  </Actions>\r\r\n'
    b'</Task>'
)

XML_MONITOR = (
    b'<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
    b'<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\r\n'
    b'  <RegistrationInfo>\r\r\n'
    b'    <URI>\\GAD E2E Test Monitor</URI>\r\r\n'
    b'  </RegistrationInfo>\r\r\n'
    b'  <Principals>\r\r\n'
    b'    <Principal id="Author">\r\r\n'
    b'      <UserId>S-1-5-21-1000-2000-3000-1001</UserId>\r\r\n'
    b'      <LogonType>InteractiveToken</LogonType>\r\r\n'
    b'    </Principal>\r\r\n'
    b'  </Principals>\r\r\n'
    b'  <Settings>\r\r\n'
    b'    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\r\n'
    b'    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\r\n'
    b'    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\r\r\n'
    b'    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\r\n'
    b'    <IdleSettings>\r\r\n'
    b'      <StopOnIdleEnd>false</StopOnIdleEnd>\r\r\n'
    b'      <RestartOnIdle>false</RestartOnIdle>\r\r\n'
    b'    </IdleSettings>\r\r\n'
    b'  </Settings>\r\r\n'
    b'  <Triggers>\r\r\n'
    b'    <LogonTrigger>\r\r\n'
    b'      <Delay>PT1M</Delay>\r\r\n'
    b'      <UserId>PC\\user</UserId>\r\r\n'
    b'    </LogonTrigger>\r\r\n'
    b'  </Triggers>\r\r\n'
    b'  <Actions Context="Author">\r\r\n'
    b'    <Exec>\r\r\n'
    b'      <Command>C:\\Python314\\pythonw.exe</Command>\r\r\n'
    b'      <Arguments>"C:\\App\\app\\gmail_app.py"</Arguments>\r\r\n'
    b'    </Exec>\r\r\n'
    b'  </Actions>\r\r\n'
    b'</Task>'
)

XML_WATCHDOG = (
    b'<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
    b'<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\r\n'
    b'  <RegistrationInfo>\r\r\n'
    b'    <URI>\\GAD E2E Test Watchdog</URI>\r\r\n'
    b'  </RegistrationInfo>\r\r\n'
    b'  <Principals>\r\r\n'
    b'    <Principal id="Author">\r\r\n'
    b'      <UserId>S-1-5-21-1000-2000-3000-1001</UserId>\r\r\n'
    b'      <LogonType>InteractiveToken</LogonType>\r\r\n'
    b'    </Principal>\r\r\n'
    b'  </Principals>\r\r\n'
    b'  <Settings>\r\r\n'
    b'    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\r\n'
    b'    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\r\n'
    b'    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>\r\r\n'
    b'    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\r\n'
    b'    <IdleSettings>\r\r\n'
    b'      <StopOnIdleEnd>false</StopOnIdleEnd>\r\r\n'
    b'      <RestartOnIdle>false</RestartOnIdle>\r\r\n'
    b'    </IdleSettings>\r\r\n'
    b'  </Settings>\r\r\n'
    b'  <Triggers>\r\r\n'
    b'    <TimeTrigger>\r\r\n'
    b'      <StartBoundary>2026-09-24T00:51:55</StartBoundary>\r\r\n'
    b'      <Repetition>\r\r\n'
    b'        <Interval>PT5M</Interval>\r\r\n'
    b'      </Repetition>\r\r\n'
    b'    </TimeTrigger>\r\r\n'
    b'  </Triggers>\r\r\n'
    b'  <Actions Context="Author">\r\r\n'
    b'    <Exec>\r\r\n'
    b'      <Command>C:\\Python314\\pythonw.exe</Command>\r\r\n'
    b'      <Arguments>"C:\\App\\app\\watchdog.py"</Arguments>\r\r\n'
    b'    </Exec>\r\r\n'
    b'  </Actions>\r\r\n'
    b'</Task>'
)

# Not a capture: registering /SC ONLOGON with /RU /IT needs elevation, so the
# non-elevated E2E could not read one back. Derived from LEGACY_WATCHDOG by
# swapping in the any-user logon trigger that /SC ONLOGON /DELAY 0001:00 creates.
LEGACY_MONITOR = (
    LEGACY_WATCHDOG.replace(
        b"    <TimeTrigger>\r\r\n"
        b"      <StartBoundary>2026-09-24T00:51:00</StartBoundary>\r\r\n"
        b"      <Repetition>\r\r\n"
        b"        <Interval>PT5M</Interval>\r\r\n"
        b"      </Repetition>\r\r\n"
        b"    </TimeTrigger>\r\r\n",
        b"    <LogonTrigger>\r\r\n"
        b"      <Delay>PT1M</Delay>\r\r\n"
        b"    </LogonTrigger>\r\r\n",
    )
    .replace(b"GAD E2E Test Watchdog", b"GAD E2E Test Monitor")
    .replace(rb"C:\App\app\watchdog.py", rb"C:\App\app\gmail_app.py")
)
