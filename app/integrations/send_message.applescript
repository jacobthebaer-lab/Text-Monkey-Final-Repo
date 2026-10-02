-- Phone and body are arguments, never interpolated into executable script.
on run argv
    if (count of argv) is not 2 then error "Expected a phone number and message"
    set recipientPhone to item 1 of argv
    set messageBody to item 2 of argv
    tell application "Messages"
        set targetAccount to first account whose service type is iMessage and enabled is true
        set targetParticipant to participant recipientPhone of targetAccount
        send messageBody to targetParticipant
    end tell
    return "submitted"
end run
