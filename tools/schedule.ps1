# Register or remove the daily AI Visibility Audit of the watchlist.
#
#   tools\schedule.ps1 install     register it, running each day at 07:30
#   tools\schedule.ps1 status      show whether it exists and when it last ran
#   tools\schedule.ps1 remove      unregister it
#
# Windows Task Scheduler rather than a daemon, mirroring the platform convention,
# because a second scheduling mechanism is a second thing to forget about.
#
# Pick an hour nothing else on the machine uses. An audit launches Chromium up
# to four times, so it must not overlap another heavy scheduled job.
#
# The job skips rather than queues if someone is already running an audit by
# hand. -StartWhenAvailable means a missed 07:30 fires on wake, which is when
# the OS actually gets opened.

param([Parameter(Position = 0)][string]$Mode = "status")

# Without this a failing cmdlet is non-terminating, execution falls through, and
# the script cheerfully prints "registered" for a task that does not exist.
$ErrorActionPreference = "Stop"

$taskName = "AI Visibility Audit"
$root = Split-Path -Parent $PSScriptRoot

# The claude-seo venv, which is where playwright and the bundled Chromium live.
# Override with $env:CLAUDE_SEO_HOME if the skill is installed elsewhere.
$suite = if ($env:CLAUDE_SEO_HOME) { $env:CLAUDE_SEO_HOME }
         else { Join-Path $env:USERPROFILE '.claude\skills\seo' }
$python = Join-Path $suite '.venv\Scripts\python.exe'

# The parameter is $Mode, not $Action, on purpose: PowerShell variable names are
# case-insensitive, so a local $action and a parameter $Action are the same
# variable. Assigning the task action overwrote the parameter, and the resulting
# type error pointed nowhere near the real cause.

switch ($Mode) {
    "install" {
        if (-not (Test-Path $python)) {
            throw "claude-seo venv python not found at $python"
        }

        $taskAction = New-ScheduledTaskAction -Execute $python `
            -Argument "-m bof.monitor --run" -WorkingDirectory $root
        $taskTrigger = New-ScheduledTaskTrigger -Daily -At 7:30am
        $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 2)

        # An explicit current-user principal registers without elevation.
        # Omitting it targets the root task folder, which needs admin and fails
        # with a bare "Access is denied" that says nothing about why.
        $me = "$env:USERDOMAIN\$env:USERNAME"
        $taskPrincipal = New-ScheduledTaskPrincipal -UserId $me `
            -LogonType Interactive -RunLevel Limited

        # -ErrorAction Stop explicitly, not just $ErrorActionPreference. These are
        # CIM cmdlets and the preference variable does not reliably make their
        # errors terminating: without this the script prints "registered" for a
        # task that does not exist, which is exactly the failure it is supposed
        # to prevent. Observed, not theoretical.
        try {
            Register-ScheduledTask -TaskName $taskName -Action $taskAction `
                -Trigger $taskTrigger -Settings $taskSettings -Principal $taskPrincipal `
                -Description "Audit every watchlist URL for AI visibility, one at a time." `
                -Force -ErrorAction Stop | Out-Null
        } catch {
            Write-Output "could not register '$taskName': $($_.Exception.Message)"
            Write-Output ""
            Write-Output "Access denied usually means this shell cannot write to the task"
            Write-Output "folder. Run the same command from a normal PowerShell window:"
            Write-Output "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" install"
            exit 1
        }

        # Verify rather than assume. Register-ScheduledTask can succeed and still
        # leave nothing queryable if the name or principal is off.
        $check = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        Write-Output "registered '$taskName', state=$($check.State), next run $((Get-ScheduledTaskInfo -TaskName $taskName).NextRunTime)"
        Write-Output "watchlist: $python -m bof.monitor --list   (run from $root)"
        Write-Output "It skips with exit 0 if you are already running an audit by hand."
    }
    "remove" {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "removed '$taskName'"
    }
    default {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Output "'$taskName' is not registered. Run: tools\schedule.ps1 install"
            break
        }
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        Write-Output "state      : $($task.State)"
        Write-Output "last run   : $($info.LastRunTime)  result=$($info.LastTaskResult)"
        Write-Output "next run   : $($info.NextRunTime)"
    }
}
