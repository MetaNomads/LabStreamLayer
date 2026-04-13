on run
    tell application "Finder"
        set appPath to (path to me) as text
        set appFolder to container of (appPath as alias) as alias
    end tell

    set projectFolder to appFolder
    set cmdFile to (projectFolder as text) & "scripts:run.command"

    tell application "Terminal"
        activate
        do script "cd " & quoted form of POSIX path of projectFolder & " && bash " & quoted form of POSIX path of (cmdFile as alias)
    end tell
end run
