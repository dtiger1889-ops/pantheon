# pantheon_toast.ps1 -- one Windows toast, no BurntToast dependency. Started non-blocking via Popen from
# pantheon/notify/channels.py:toast; never prints, always exits 0 so a broken toast never
# shows up as anything but a missing notification.
#   pantheon_toast.ps1 -Title "Claude needs you" -Body "permission in loom-os (window 3), 1m"
param(
    [string]$Title = "Pantheon",
    [string]$Body = ""
)
$ErrorActionPreference = 'SilentlyContinue'
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $text = $template.GetElementsByTagName("text")
    $text.Item(0).AppendChild($template.CreateTextNode($Title)) | Out-Null
    $text.Item(1).AppendChild($template.CreateTextNode($Body)) | Out-Null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    try {
        # "Pantheon" is not a registered AUMID on most machines, so this usually falls through
        # to the AUMID that is always present: PowerShell's own.
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Pantheon").Show($toast)
    } catch {
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Windows PowerShell").Show($toast)
    }
} catch { }
exit 0
