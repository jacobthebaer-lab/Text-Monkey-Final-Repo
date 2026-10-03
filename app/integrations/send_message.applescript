-- Phone and body are arguments, never interpolated into executable script.
on run argv
    if (count of argv) is not 2 and (count of argv) is not 3 then error "Expected a phone number, message and optional direct chat"
    set recipientPhone to item 1 of argv
    set messageBody to item 2 of argv
    tell application "Messages"
        if (count of argv) is 3 then
            set targetChat to chat id (item 3 of argv)
            send messageBody to targetChat
        else
            set targetAccount to first account whose service type is iMessage and enabled is true
            set targetParticipant to participant recipientPhone of targetAccount
            send messageBody to targetParticipant
        end if
    end tell
    return "submitted"
end run
