# Start the whole local stack: two RQ workers, the scheduler, the API.
#
# This exists because launch commands are where this project keeps breaking.
# Three separate outages came from a hand-typed or copy-pasted command line,
# not from the code: a queue name that outlived a rename (thirteen hours of
# dead intake), a duplicate scheduler started from an old venv (48 hours of
# doubled eBay quota), and workers holding code older than the migrations they
# were writing against. A script that is the single way to start things makes
# all three harder to reproduce.
#
#   powershell -File scripts/start-local.ps1          # start everything
#   powershell -File scripts/start-local.ps1 -Stop    # stop everything first
#
# Queue names are NOT written here. systems/rqworker.py imports them from
# systems/queue.py, which is the whole point of that shim.

param(
    [switch]$Stop,
    [string]$Venv = 'C:\venvs\deal-finder',
    [string]$LogDir = "$env:LOCALAPPDATA\undercut\logs"
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $Venv 'Scripts\python.exe'

if (-not (Test-Path $py)) {
    throw "No interpreter at $py. Set -Venv to wherever UV_PROJECT_ENVIRONMENT points."
}

# Stop anything already running from this repo. Matching on the command line
# rather than the process name matters: `uv run` wrappers mean one logical
# worker shows up as two python.exe processes, and a stale duplicate is
# indistinguishable from a healthy one by name alone.
$patterns = 'systems.rqworker', 'systems.scheduler', 'api.main:app', 'rqworker.py'
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $c = $_.CommandLine; $c -and ($patterns | Where-Object { $c -like "*$_*" }) }

if ($running) {
    # NOTE: this is a hard kill, so any job a worker is midway through is
    # abandoned. RQ notices and moves it to the failed registry as an
    # AbandonedJobError, and the scheduler re-enqueues it on its next tick, so
    # nothing is permanently lost. But it does mean a restart leaves failures
    # behind that look like real ones, and the failed registry is supposed to
    # be a meaningful signal (a silently empty one hid an outage for hours in
    # August). If the registry is non-empty after a restart, check whether the
    # entries are AbandonedJobError with a timestamp matching the restart
    # before treating them as a bug.
    #
    # A graceful stop would be better and is awkward here: RQ shuts down
    # cleanly on SIGTERM, which Windows does not really have, and
    # Stop-Process is effectively SIGKILL.
    Write-Output "stopping $($running.Count) existing process(es)"
    # Every process in a `uv run` pair matches the pattern, so killing each
    # individually covers the tree. Failures are ignored on purpose: killing a
    # parent takes its child with it, and the child is then already gone by
    # the time its own turn comes round.
    foreach ($p in $running) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
    }
    Start-Sleep -Seconds 3
}

if ($Stop) {
    Write-Output 'stopped.'
    exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONPATH = $repo
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# Exactly one of each. Two schedulers means two sets of ingest jobs against a
# 5,000 call/day budget, which is how a day's quota vanished on 2026-08-05.
$services = @(
    @{ Name = 'worker-main'; Args = @('-u', '-m', 'systems.rqworker', 'main') },
    @{ Name = 'worker-ml';   Args = @('-u', '-m', 'systems.rqworker', 'ml') },
    @{ Name = 'scheduler';   Args = @('-u', '-m', 'systems.scheduler') },
    @{ Name = 'api';         Args = @('-u', '-m', 'uvicorn', 'api.main:app', '--port', '8000') }
)

foreach ($s in $services) {
    $out = Join-Path $LogDir "$($s.Name)-$stamp.log"
    $proc = Start-Process -FilePath $py -ArgumentList $s.Args -WorkingDirectory $repo -RedirectStandardOutput $out -RedirectStandardError "$out.err" -WindowStyle Hidden -PassThru
    Write-Output "$($s.Name) pid=$($proc.Id) log=$out"
}

Write-Output ''
Write-Output 'Check liveness with:  python -m systems.scheduler_health'
Write-Output 'Process names lie here; that command reads the Redis heartbeat.'
