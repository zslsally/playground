property subjectMarker : "iPhone 笔记本"
property attachmentSuffix : " - Notebook.html"

using terms from application "Mail"
	on perform mail action with messages theMessages for rule theRule
		my processMessages(theMessages)
	end perform mail action with messages
end using terms from

on run
	tell application "Mail" to set selectedMessages to selection
	if (count of selectedMessages) is 0 then error "请先在 Mail 中选择一封 Kindle 导出邮件。"
	processMessages(selectedMessages)
end run

on processMessages(theMessages)
	set homeRoot to do shell script "/usr/bin/printenv HOME"
	set skillRoot to homeRoot & "/.codex/skills/convert-kindle-notebook"
	set converterPath to skillRoot & "/scripts/convert_kindle_notebook.py"
	set configPath to skillRoot & "/config.local.json"
	set inboxRoot to my expandHome(homeRoot, my configValue(configPath, "mail_inbox"))
	set expectedSender to my configValue(configPath, "mail_sender")
	set expectedRecipient to my configValue(configPath, "mail_recipient")
	set incomingFolder to inboxRoot & "/incoming"
	set processedFolder to inboxRoot & "/processed"
	set failedFolder to inboxRoot & "/failed"
	set logPath to inboxRoot & "/import.log"
	do shell script "/bin/mkdir -p " & quoted form of incomingFolder & space & quoted form of processedFolder & space & quoted form of failedFolder

	repeat with theMessage in theMessages
		tell application "Mail"
			set subjectText to subject of theMessage
			set senderText to sender of theMessage
			set recipientAddresses to address of every to recipient of theMessage
			set messageAttachments to mail attachments of theMessage
		end tell

		if senderText contains expectedSender and recipientAddresses contains expectedRecipient and subjectText contains subjectMarker then
			set foundNotebook to false
			set messageSucceeded to true
			repeat with theAttachment in messageAttachments
				tell application "Mail" to set attachmentName to name of theAttachment
				if my endsWith(attachmentName, attachmentSuffix) then
					set foundNotebook to true
					set safeName to my replaceText("/", "／", attachmentName)
					set timestampText to do shell script "/bin/date '+%Y%m%d-%H%M%S'"
					set inputPath to incomingFolder & "/" & timestampText & " - " & safeName

					try
						tell application "Mail" to save theAttachment in POSIX file inputPath
						set conversionOutput to do shell script "/usr/bin/python3 " & quoted form of converterPath & space & quoted form of inputPath & " --write"
						set archivedPath to processedFolder & "/" & timestampText & " - " & safeName
						do shell script "/bin/mv " & quoted form of inputPath & space & quoted form of archivedPath
						my appendLog(logPath, timestampText & tab & "SUCCESS" & tab & attachmentName & linefeed & conversionOutput & linefeed)
						my showNotification(attachmentName, "Kindle → Obsidian 已完成")
					on error errorMessage number errorNumber
						set messageSucceeded to false
						set failedPath to failedFolder & "/" & timestampText & " - " & safeName
						try
							do shell script "/usr/bin/test ! -e " & quoted form of inputPath & " || /bin/mv " & quoted form of inputPath & space & quoted form of failedPath
						end try
						my appendLog(logPath, timestampText & tab & "FAILED" & tab & attachmentName & tab & errorNumber & tab & errorMessage & linefeed)
						my showNotification(errorMessage, "Kindle → Obsidian 转换失败")
					end try
				end if
			end repeat

			if foundNotebook and messageSucceeded then
				tell application "Mail"
					set read status of theMessage to true
					delete theMessage
				end tell
			end if
		end if
	end repeat
end processMessages

on configValue(configPath, configKey)
	return do shell script "/usr/bin/plutil -extract " & quoted form of configKey & " raw -o - " & quoted form of configPath
end configValue

on expandHome(homeRoot, pathText)
	if pathText begins with "~/" then return homeRoot & "/" & (text 3 thru -1 of pathText)
	return pathText
end expandHome

on endsWith(theText, theSuffix)
	if (count of theText) < (count of theSuffix) then return false
	return text (-(count of theSuffix)) thru -1 of theText is theSuffix
end endsWith

on replaceText(findText, replacementText, sourceText)
	set previousDelimiters to AppleScript's text item delimiters
	try
		set AppleScript's text item delimiters to findText
		set textItems to text items of sourceText
		set AppleScript's text item delimiters to replacementText
		set resultText to textItems as text
		set AppleScript's text item delimiters to previousDelimiters
		return resultText
	on error errorMessage number errorNumber
		set AppleScript's text item delimiters to previousDelimiters
		error errorMessage number errorNumber
	end try
end replaceText

on showNotification(messageText, titleText)
	try
		display notification messageText with title titleText
	end try
end showNotification

on appendLog(logPath, logEntry)
	set logFile to POSIX file logPath
	set fileHandle to missing value
	try
		set fileHandle to open for access logFile with write permission
		write logEntry to fileHandle starting at eof
		close access fileHandle
	on error
		if fileHandle is not missing value then close access fileHandle
	end try
end appendLog
