"""Trigger phrases: what a person might actually type for each capability.

Kept out of `catalog.py` so the catalogue stays a description of *what the code
can do*, and this stays a description of *how people ask*. A capability is
reachable by its action name at all times; these phrases only decide whether a
loose sentence is routed to it without asking the model first.

Three registers are covered for every common request, because the tool is used
in all three:

* English      — "mute the sound", "empty the recycle bin"
* Hindi        — "आवाज़ बंद करो", "रीसायकल बिन डालो"
* Hinglish    — "sound mute kar do", "downloads saaf kar do"

Hindi is written in Devanagari *and* transliterated, because a request arriving
by voice is transliterated long before anyone types it.
"""
from __future__ import annotations

# category -> (capability names, [phrases])
PHRASES: dict[int, tuple[tuple[str, ...], list[str]]] = {
    1: [
        (("app_list",), [
            "list installed apps", "installed programs", "what apps are installed",
            "kay apps install hain", "installed apps batao", "कौन से ऐप्स इंस्टॉल हैं",
        ]),

        (("app_install",), [
            "install an app", "install <name>", "download and install",
            "app install karo", "install kar do", "ऐप इंस्टॉल करो", "यह प्रोग्राम इंस्टॉल कर दो",
            "install", "install karo", "इंस्टॉल करो",
        ]),

        (("app_uninstall",), [
            "uninstall an app", "remove a program", "delete an app",
            "app uninstall karo", "हटा दो यह प्रोग्राम", "प्रोग्राम हटाओ",
            "uninstall", "uninstall karo", "अनइंस्टॉल करो",
        ]),

        (("app_update_all",), [
            "update all apps", "update everything", "update my programs",
            "sab apps update karo", "सब अपडेट कर दो", "सारे प्रोग्राम अपडेट करो",
        ]),

        (("app_startup_list",), [
            "startup apps", "what starts with windows", "startup impact",
            "startup programs", "startup me kya chal raha hai", "स्टार्टअप ऐप्स",
        ]),

        (("app_startup_add",), [
            "start this on startup", "run at startup", "add to startup",
            "startup pe chalao", "स्टार्टअप में चलाओ",
        ]),

        (("app_bloatware",), [
            "remove bloatware", "get rid of preinstalled apps", "clean up windows apps",
            "bloatware hatao", "पहले से लगे ऐप्स हटाओ",
        ]),

        (("app_pin",), [
            "pin to taskbar", "pin this app", "taskbar pe pin karo", "टास्कबार पर पिन करो",
        ]),

        (("app_run_admin",), [
            "run as administrator", "open as admin", "admin ke naam se kholo",
            "एडमिन के रूप में खोलो",
        ]),

        (("app_default_set",), [
            "set default app", "make this the default browser",
            "default app badlo", "डिफ़ॉल्ट ऐप बदलो",
        ]),

        (("window_manage",), [
            "minimize all", "snap left", "snap right", "maximize the window",
            "minimise everything", "show desktop", "move window to other monitor",
            "sab windows chhupao", "बाईं तरफ सटका दो", "सब बंद करो मिनिमाइज",
        ]),

    ],
    2: [
        (("screen_capture",), [
            "take a screenshot", "screenshot lo", "screen capture karo",
            "स्क्रीनशॉट लो", "स्क्रीन का फोटो लो",
        ]),

        (("screen_describe",), [
            "what is on my screen", "describe the screen", "read the screen",
            "screen pe kya hai", "screen par kya hai", "स्क्रीन पर क्या है",
        ]),

        (("screen_read_text",), [
            "read the text on screen", "ocr this", "screen text padho",
            "स्क्रीन का टेक्स्ट पढ़ो",
        ]),

        (("screen_wait_for",), [
            "wait until it appears", "wait for the dialog", "wait for it to load",
            "tab tak wait karo", "इंतजार करो जब तक",
        ]),

        (("screen_find_click",), [
            "find and click", "click the button that says", "click on the text",
            "dhundho aur click karo", "ढूंढकर क्लिक करो",
        ]),

        (("keyboard_type",), [
            "type this text", "press enter", "hit ctrl+s", "keyboard shortcut",
            "ye type karo", "टाइप करो", "की दबाओ",
        ]),

        (("mouse_click",), [
            "click on it", "click the button", "click this", "press the button",
            "click karo", "बटन दबाओ", "क्लिक करो",
        ]),
        (("mouse_double_click",), [
            "double click", "double-click it", "open it with a double click",
            "double click karo", "डबल क्लिक करो",
        ]),
        (("mouse_right_click",), [
            "right click", "right-click it", "open the context menu",
            "right click karo", "राइट क्लिक करो",
        ]),
        (("mouse_hover",), [
            "hover over it", "move the mouse over", "hover karo",
        ]),
        (("mouse_drag",), [
            "drag it", "drag and drop", "drag the file", "drop it here",
            "drag karo", "खींचो",
        ]),
        (("mouse_scroll",), [
            "scroll down", "scroll up", "scroll the page", "scroll a bit",
            "scroll karo", "स्क्रॉल करो",
        ]),
        (("gui_invoke",), [
            "press the button properly", "invoke the control", "activate it",
        ]),
        (("gui_get_text",), [
            "read that control", "what does the field say", "get the text of",
        ]),
        (("gui_set_text",), [
            "set the text of", "replace the text in", "fill the field with",
        ]),
        (("gui_append_text",), [
            "append to the text", "add to that field",
        ]),
        (("gui_select",), [
            "pick from the list", "choose that item", "select from the dropdown",
        ]),
        (("gui_toggle",), [
            "tick that box", "untick it", "check the checkbox", "uncheck it",
        ]),
        (("gui_slider",), [
            "move the slider", "drag the slider to", "set the slider to",
        ]),
        (("gui_expand",), [
            "expand that section", "open the tree node", "expand it",
        ]),
        (("gui_collapse",), [
            "collapse that section", "close the tree node", "collapse it",
        ]),
        (("keyboard_select_all",), [
            "select all", "select everything", "ctrl+a", "सब चुनो",
        ]),
        (("keyboard_copy",), ["copy it", "copy this", "ctrl+c", "कॉपी करो"]),
        (("keyboard_paste",), ["paste it", "paste this", "ctrl+v", "पेस्ट करो"]),
        (("keyboard_hotkey",), [
            "press the shortcut", "hit the keys", "press ctrl+", "press alt+",
            "shortcut dabao", "शॉर्टकट दबाओ",
        ]),
        (("screen_annotate",), [
            "annotate the screenshot", "show me the controls", "number the controls",
            "labels dikhao",
        ]),

    ],
    3: [
        (("file_find",), [
            "find a file", "search for", "where is that file", "locate the file",
            "file dhoondho", "फ़ाइल ढूंढो",
        ]),

        (("file_organise",), [
            "clean up my downloads", "sort downloads", "find duplicate files",
            "find big files", "organize my desktop", "delete duplicates",
            "downloads saaf karo", "duplicates hatao", "डाउनलोड साफ़ करो",
            "डुप्लीकेट फाइलें हटाओ", "डेस्कटॉप साफ करो",
        ]),

        (("file_batch_rename",), [
            "rename all files", "batch rename", "rename every jpg to png",
            "rename every file", "rename files", "rename each file",
            "rename every", "rename all",
            "sab files rename karo", "सभी फाइलों का नाम बदलो",
        ]),

        (("file_bulk_move",), [
            "move all these files", "sort photos into folders by date",
            "ye files yahan move karo", "फाइलें यहाँ ले जाओ",
        ]),

        (("file_encrypt",), [
            "encrypt this file", "password protect a file", "decrypt the file",
            "file encrypt karo", "फाइल एन्क्रिप्ट करो",
        ]),

        (("file_recycle",), [
            "empty the recycle bin", "recycle bin khaali karo", "रीसायकल बिन खाली करो",
        ]),

        (("file_archive",), [
            "zip this folder", "unzip it", "compress the files",
            "zip karo", "unzip karo", "ज़िप करो", "अनज़िप करो",
        ]),

    ],
    4: [
        (("doc_create",), [
            "create a word document", "make a powerpoint", "create a spreadsheet",
            "new pdf", "word document banao", "ppt बनाओ", "वर्ड डॉक्यूमेंट बनाओ",
        ]),

        (("doc_edit",), [
            "summarise this document", "proofread it", "translate this document",
            "edit the document", "document summarize karo", "इसका सारांश बताओ",
            "दस्तावेज़ का सारांश निकालो",
        ]),

        (("doc_convert",), [
            "convert pdf to word", "convert to pdf", "change the format",
            "pdf se word", "pdf mein convert karo", "पीडीएफ़ को वर्ड में बदलो",
        ]),

        (("doc_pdf_merge",), [
            "merge these pdfs", "join the pdf files", "pdf jodo", "पीडीएफ़ मिलाओ",
        ]),

        (("doc_pdf_split",), [
            "split this pdf", "extract page 3", "pdf ke pages alag karo",
            "पीडीएफ़ के पेज अलग करो",
        ]),

        (("doc_form_fill",), [
            "fill this form", "fill the pdf form", "form bhar do", "फ़ॉर्म भर दो",
        ]),

        (("doc_chart",), [
            "make a chart", "plot this data", "graph banao", "चार्ट बनाओ",
        ]),

        (("doc_template",), [
            "draft an invoice", "write a letter", "make a report",
            "invoice banao", "रेखा तैयार करो", "रिपोर्ट लिखो",
        ]),

    ],
    5: [
        (("clip_get",), ["read clipboard", "what is on my clipboard",
                        "clipboard kya hai", "क्लिपबोर्ड क्या है"]),

        (("clip_set",), ["copy this to clipboard", "clipboard me copy karo",
                        "क्लिपबोर्ड पर कॉपी करो"]),

        (("clip_history",), ["clipboard history", "clipboard ki history",
                            "क्लिपबोर्ड हिस्ट्री"]),

        (("clip_clear",), ["clear the clipboard", "clear my clipboard",
                           "empty the clipboard", "wipe the clipboard",
                           "clipboard saaf karo", "clipboard khaali karo",
                           "क्लिपबोर्ड साफ करो"]),

        (("text_case",), ["uppercase this", "lowercase this", "title case",
                         "case badlo", "अक्षर बदलो"]),

        (("text_count",), ["count the words", "how many words",
                          "words ginno", "शब्द गिनो"]),

        (("text_transform",), ["remove duplicate lines", "sort these lines",
                              "lines sort karo", "लाइनें क्रमबद्ध करो"]),

        (("text_replace",), ["find and replace in all files",
                            "replace text everywhere", "सब फ़ाइलों में बदलो"]),

        (("text_format_json",), ["format this json", "pretty print json",
                                "json साफ़ करो"]),

    ],
    6: [
        (("web_open",), ["open a website", "go to", "browse to", "site kholo",
                        "वेबसाइट खोलो"]),

        (("web_search",), ["search the web", "google this", "search karo",
                          "इंटरनेट पर खोजो"]),

        (("web_tabs",), ["new tab", "close tab", "next tab", "reopen closed tab",
                        "tab kholo", "टैब खोलो"]),

        (("web_bookmark",), ["bookmark this", "add bookmark", "bookmark karo",
                            "बुकमार्क करो"]),

        (("web_download",), ["download this", "download the file",
                            "download karo", "डाउनलोड करो"]),

        (("web_fill_form",), ["fill this form", "form bhar do", "फ़ॉर्म भरो"]),

        (("web_login",), ["log in to", "sign in", "login karo", "लॉग इन करो"]),

        (("web_purchase",), ["add to cart", "book a ticket", "book a table",
                            "buy this", "cart me daalo", "टिकट बुक करो", "खरीदो"]),

        (("web_compare",), ["compare prices", "best price dhoondho", "कीमत की तुलना करो"]),

        (("web_monitor",), ["watch this page", "tell me when the page changes",
                           "page badalne pe batao"]),

        (("web_scrape",), ["scrape this page", "get the data from this site",
                          "website se data nikalo", "साइट से डेटा निकालो"]),

        (("web_clear",), ["clear cookies", "clear browser history", "cache saaf karo",
                         "कुकीज़ हटाओ", "ब्राउज़र हिस्ट्री मिटाओ"]),

        (("web_private",), ["open a private window", "incognito kholo", "incognito खोलो"]),

        (("web_save_pdf",), ["save this page as pdf", "page pdf banao", "पेज पीडीएफ़ बनाओ"]),

        (("web_block",), ["block this website", "site block karo", "वेबसाइट ब्लॉक करो"]),

    ],
    7: [
        (("mail_list",), ["check my email", "any new mail", "unread mail",
                         "email dekho", "ईमेल देखो"]),

        (("mail_read",), ["read the email from", "summarise my email",
                         "email padho", "ईमेल पढ़ो"]),

        (("mail_search",), ["search my email", "find that email",
                           "email me dhoondho", "ईमेल में ढूंढो"]),

        (("mail_compose",), ["write an email", "reply to", "draft an email",
                            "email likho", "ईमेल लिखो"]),

        (("mail_send",), ["send the email", "email bhejo", "ईमेल भेजो"]),

        (("mail_move",), ["archive this email", "delete that email", "mark as spam",
                         "email archive karo", "ईमेल हटाओ"]),

        (("mail_attach",), ["attach a file to the email", "file attach karo",
                           "फ़ाइल अटैच करो"]),

        (("mail_unsubscribe",), ["unsubscribe me from", "band kar do is newsletter ko",
                                "न्यूज़लेट्टर से हटाओ"]),

        (("calendar_events",), ["what is on my calendar", "my schedule today",
                               "agenda kya hai", "आज का कैलेंडर दिखाओ"]),

        (("calendar_meeting",), ["schedule a meeting", "book a call", "cancel the meeting",
                                "meeting fix karo", "मीटिंग तय करो"]),

        (("calendar_remind",), ["remind me to", "set a reminder", "remind karo",
                               "याद दिलाओ"]),

        (("message_send",), ["send a whatsapp message", "message on slack",
                             "send a text message", "whatsapp message bhejo",
                             "स्लैक पर संदेश भेजो"]),

        (("notifications",), ["read my notifications", "koi notification hai",
                              "सूचनाएं पढ़ो"]),

        (("call_start",), ["call on teams", "start a video call", "video call karo",
                          "वीडियो कॉल करो"]),

    ],
    8: [
        (("meeting_control",), ["join the meeting", "mute myself", "share my screen",
                               "leave the meeting", "meeting me mute karo",
                               "स्क्रीन शेयर करो"]),

        (("meeting_notes",), ["take meeting notes", "meeting notes banao",
                             "मीटिंग नोट्स लो"]),

        (("meeting_followup",), ["send the follow up", "follow up bhejo",
                                "फॉलो अप भेजो"]),

        (("work_ticket",), ["create a jira ticket", "add to trello", "update the ticket",
                           "jira ticket banao", "टिकट बनाओ"]),

        (("work_timesheet",), ["fill my timesheet", "timesheet bharo", "टाइमशीट भरो"]),

        (("work_expense",), ["file an expense report", "expense claim do",
                            "खर्च रिपोर्ट दाखिल करो"]),

        (("work_report",), ["export the report", "report export karo", "रिपोर्ट निकालो"]),

        (("work_crm",), ["update the crm record", "crm me update karo", "सीआरएम अपडेट करो"]),

    ],
    9: [
        (("set_volume",), ["mute", "unmute", "volume badlo", "set the volume",
                          "mute karo", "आवाज़ बंद करो", "आवाज़ चालू करो"]),

        (("set_brightness",), ["brightness badlo", "dim the screen", "screen bright karo",
                              "स्क्रीन की रोशनी बदलो"]),

        (("set_theme",), ["dark mode", "light mode", "night theme", "theme badlo",
                         "डार्क मोड", "हल्का थीम"]),

        (("set_night_light",), ["night light on", "blue light filter",
                               "नाइट लाइट चालू करो"]),

        (("set_do_not_disturb",), ["do not disturb", "disturb mat karo", "डीएनडी चालू करो"]),

        (("set_wifi",), ["turn on wifi", "wifi off karo", "वाईफ़ाई बंद करो"]),

        (("set_bluetooth",), ["turn off bluetooth", "bluetooth off karo", "ब्लूटूथ बंद करो"]),

        (("set_airplane",), ["airplane mode", "flight mode on", "एयरप्लेन मोड"]),

        (("set_wallpaper",), ["change the wallpaper", "desktop background badlo",
                             "वॉलपेपर बदलो"]),

        (("set_display",), ["change resolution", "set the scaling", "resolution badlo",
                           "स्क्रीन रिज़ॉल्यूशन बदलो"]),

        (("set_power_plan",), ["power plan badlo", "battery saver on",
                              "पावर प्लान बदलो"]),

        (("set_power",), ["shut down", "restart the computer", "sleep now",
                          "hibernate", "lock the computer",
                          "band karo computer", "restart karo", "कंप्यूटर बंद करो",
                          "सो जाओ",
                          "restart", "reboot", "shutdown", "sleep",
                          "band karo", "बंद करो"]),

        (("set_printer_default",), ["set default printer", "default printer badlo",
                                   "डिफ़ॉल्ट प्रिंटर बदलो"]),

        (("set_language",), ["change keyboard layout", "install hindi keyboard",
                            "language badlo", "भाषा बदलो"]),

        (("set_time",), ["what time is it", "change the timezone", "time badlo",
                        "समय बदलो"]),

        (("set_proxy",), ["set the proxy", "proxy badlo", "प्रॉक्सी सेट करो"]),

        (("set_dns",), ["change dns", "dns server badlo", "डीएनएस बदलो"]),

        (("windows_update",), ["check for windows updates", "update windows",
                              "windows update karo", "विंडोज़ अपडेट करो"]),

    ],
    10: [
        (("hw_status",), ["how is my computer doing", "cpu usage", "ram usage",
                         "performance dikhao", "कंप्यूटर कैसा चल रहा है"]),

        (("hw_processes",), ["what is using my cpu", "top processes",
                            "kaunsa app zyada ram lelo raha hai", "सबसे ज़्यादा राम कौन ले रहा है"]),

        (("hw_kill_process",), ["kill this process", "end task", "task manager se band karo",
                               "प्रोसेस ख़त्म करो"]),

        (("hw_temperature",), ["cpu temperature", "how hot is it", "तापमान बताओ"]),

        (("hw_battery",), ["battery status", "kitni charge hai", "बैटरी कितनी है"]),

        (("hw_ip",), ["what is my ip", "my ip address", "mera ip kya hai", "आईपी पता बताओ"]),

        (("hw_devices",), ["connected devices", "usb devices", "कौन से डिवाइस जुड़े हैं"]),

        (("hw_eject",), ["eject the usb", "usb nikal do", "यूएसबी निकालो"]),

        (("hw_free_port",), ["free port 3000", "port khaali karo", "पोर्ट खाली करो"]),

        (("hw_disk_check",), ["check the disk for errors", "disk check karo", "डिस्क जाँचो"]),

        (("hw_speedtest",), ["test my internet speed", "internet speed kitni hai",
                            "स्पीड टेस्ट करो"]),

        (("hw_storage_report",), ["what is using my disk space", "storage report",
                                  "disk me kitna space gaya", "डिस्क स्पेस रिपोर्ट",
                                  "how much disk space is left", "free up disk space",
                                  "disk space khatam ho raha hai",
                                  "डिस्क में जगह कम है"]),

        (("hw_system_info",), ["system information", "pc ki jaankari",
                              "सिस्टम इंफ़ो"]),

        (("hw_drivers",), ["update the drivers", "driver update karo", "ड्राइवर अपडेट करो"]),

        (("hw_health",), ["why is my computer crashing", "hardware errors",
                         "reliability history", "हार्डवेयर एरर"]),

    ],
    11: [
        (("net_wifi_list",), ["what wifi networks are available", "wifi networks dikhao",
                             "वाईफ़ाई नेटवर्क दिखाओ"]),

        (("net_wifi_connect",), ["connect to wifi", "wifi se jodo", "वाईफ़ाई से जुड़ो"]),

        (("net_wifi_forget",), ["forget this network", "network bhool jao",
                               "नेटवर्क भूल जाओ"]),

        (("net_hotspot",), ["turn on hotspot", "mobile hotspot", "हॉटस्पॉट चालू करो"]),

        (("net_ping",), ["ping google", "test the connection to", "ping karo"]),

        (("net_flush_dns",), ["flush dns", "dns cache saaf karo", "डीएनएस कैश साफ़ करो"]),

        (("net_devices",), ["what devices are on my network", "network me kaun kaun hai",
                           "नेटवर्क के उपकरण"]),

        (("net_block_device",), ["block that device", "device block karo", "डिवाइस ब्लॉक करो"]),

        (("net_port_check",), ["is port 8080 open", "port check karo"]),

        (("net_vpn",), ["connect the vpn", "turn on vpn", "vpn chalu karo", "वीपीएन चालू करो"]),

        (("net_static_ip",), ["set a static ip", "static ip set karo", "स्टैटिक आईपी सेट करो"]),

        (("net_fix",), ["it says no internet", "wifi connected but no internet",
                       "internet nahi chal raha", "इंटरनेट नहीं चल रहा"]),

        (("net_reset",), ["reset the network", "network reset karo", "नेटवर्क रीसेट करो"]),

        (("net_profile",), ["show my network", "ip config", "नेटवर्क जानकारी"]),

    ],
    12: [
        (("fix_diagnose",), ["why is my computer slow", "diagnose this",
                            "computer slow kyun hai", "मेरा कंप्यूटर धीमा क्यों है"]),

        (("fix_speed_up",), ["make my laptop faster", "speed up my pc",
                            "computer tez karo", "मेरा लैपटॉप तेज़ करो",
                            "कंप्यूटर तेज़ कर दो"]),

        (("fix_reset_network",), ["reset the network adapter", "adapter reset karo"]),

        (("fix_sfc_dism",), ["run sfc", "repair system files", "sfc chalao",
                            "सिस्टम फ़ाइलें ठीक करो"]),

        (("fix_events",), ["check the event log", "event viewer", "इवेंट लॉग देखो"]),

        (("fix_crash_cause",), ["what does this error mean", "app crash ho raha hai kyun",
                               "यह एरर क्यों आ रहा है"]),

        (("fix_clear_cache",), ["clear the cache", "cache saaf karo", "कैश साफ़ करो"]),

        (("fix_office",), ["repair office", "word will not open", "ऑफ़िस ठीक करो"]),

        (("fix_reset_app",), ["reset this app", "app reset karo", "ऐप रीसेट करो"]),

        (("fix_rollback_driver",), ["roll back the driver", "driver rollback karo"]),

        (("fix_safe_mode",), ["start in safe mode", "safe mode me chalao", "सेफ़ मोड"]),

        (("fix_sound",), ["there is no sound", "audio nahi aa raha", "आवाज़ नहीं आ रही"]),

        (("fix_printer",), ["the printer is not working", "printer kaam nahi kar raha",
                           "प्रिंटर काम नहीं कर रहा"]),

        (("fix_bluetooth",), ["bluetooth is not working", "bluetooth dikh nahi raha",
                             "ब्लूटूथ काम नहीं कर रहा"]),

        (("fix_black_screen",), ["my screen is black", "screen black ho gaya",
                                "स्क्रीन काली है"]),

    ],
    13: [
        (("media_control",), ["play music", "pause the video", "next song",
                              "previous track", "video chalao", "गाना चलाओ", "वीडियो रोको"]),

        (("media_playlist",), ["make a playlist", "shuffle my music", "playlist banao",
                              "प्लेलिस्ट बनाओ"]),

        (("media_convert",), ["convert this video", "convert to mp3", "audio nikaal do",
                             "वीडियो बदलो", "ऑडियो निकालो"]),

        (("media_trim_merge",), ["trim this video", "join these clips",
                                "clip kaat do", "क्लिप काटो"]),

        (("media_extract_audio",), ["extract the audio", "audio nikalo", "ऑडियो निकालो"]),

        (("media_compress",), ["compress this video", "make the file smaller",
                              "video chhota karo", "वीडियो छोटा करो"]),

        (("media_record",), ["record my screen", "screen record karo", "स्क्रीन रिकॉर्ड करो"]),

    ],
    14: [
        (("image_edit",), ["resize this image", "crop the photo", "rotate the image",
                          "image chhota karo", "तस्वीर छोटी करो"]),

        (("image_watermark",), ["add a watermark", "watermark lagao", "वॉटरमार्क लगाओ"]),

        (("image_batch",), ["resize all photos", "batch resize", "सारी फोटो छोटी करो"]),

        (("image_creative",), ["make a thumbnail", "create a logo", "make a gif",
                              "poster banao", "थंबनेल बनाओ", "लोगो बनाओ"]),

        (("image_generate",), ["generate an image", "picture banao", "तस्वीर बनाओ"]),

        (("image_background",), ["remove the background", "background hatao",
                                "बैकग्राउंड हटाओ"]),

        (("image_enhance",), ["sharpen this photo", "make the photo better",
                             "photo sudharo", "तस्वीर सुधारो"]),

        (("image_palette",), ["what colours are in this image", "colour palette nikalo",
                             "रंग निकालो"]),

    ],
    15: [
        (("audio_record",), ["record a voice memo", "voice note banao", "आवाज़ रिकॉर्ड करो"]),

        (("audio_denoise",), ["remove the noise", "clean up this audio",
                             "awaaz saaf karo", "शोर हटाओ"]),

        (("audio_device",), ["change my speaker", "switch microphone",
                            "speaker badlo", "स्पीकर बदलो"]),

        (("audio_test_mic",), ["test my microphone", "mic check karo", "माइक जाँचो"]),

        (("audio_trim_speed",), ["speed up the audio", "trim the recording",
                                 "audio tez karo"]),

        (("speak",), ["read this aloud", "speak this", "bolo", "ज़ोर से पढ़ो", "बोल दो"]),

        (("transcribe",), ["transcribe this recording", "audio to text",
                          " Recording ka text nikalo", "ऑडियो को टेक्स्ट में बदलो"]),

        (("subtitles",), ["make subtitles", "subtitles banao", "सबटाइटल बनाओ"]),

        (("voice_commands",), ["listen to me", "voice commands on", "dictation mode",
                              "voice control chalu karo", "आवाज़ से चलाओ"]),

    ],
    16: [
        (("dev_open_project",), ["open my project", "open the code folder",
                                "project kholo", "प्रोजेक्ट खोलो"]),

        (("dev_run",), ["run the code", "chalao is project ko", "कोड चलाओ"]),

        (("dev_build",), ["build the project", "project build karo", "बिल्ड करो"]),

        (("dev_test",), ["run the tests", "tests chalao", "टेस्ट चलाओ"]),

        (("dev_deps",), ["install dependencies", "pip install", "npm install",
                        "install karo dependencies", "निर्भरताएँ इंस्टॉल करो"]),

        (("dev_venv",), ["create a virtual environment", "venv banao", "वर्चुअल एनवायरनमेंट बनाओ"]),

        (("git",), ["git commit", "git push", "git pull", "create a branch",
                   "merge the branch", "clone this repo", "git me kya hai",
                   "commit kar do", "push karo", "ब्रांच बनाओ", "कमिट करो"]),

        (("dev_server",), ["start the dev server", "server chalao", "सर्वर चलाओ"]),

        (("docker",), ["docker ps", "build the docker image", "docker compose up",
                      "docker se chalwao"]),

        (("wsl",), ["run in wsl", "wsl me chalao", "wsl ubuntu"]),

        (("ssh",), ["ssh into the server", "ssh se jodo", "ssh से जोड़ो"]),

        (("deploy",), ["deploy the app", "deploy karo", "डिप्लॉय करो"]),

        (("code_explain",), ["explain this code", "what does this file do",
                            "code samjhao", "कोड समझाओ"]),

        (("code_refactor",), ["refactor this", "code sudharo", "कोड सुधारो"]),

        (("code_tests",), ["write tests for this", "tests likho", "टेस्ट लिखो"]),

        (("code_debug",), ["why is this failing", "debug this error",
                          "error kyun aa raha hai", "एरर क्यों आ रहा है"]),

        (("code_script",), ["write a script to", "script banao jo", "स्क्रिप्ट लिखो"]),

    ],
    17: [
        (("data_clean",), ["clean this csv", "remove blank rows", "csv saaf karo",
                          "सीएसवी साफ़ करो"]),

        (("data_pivot",), ["make a pivot table", "pivot banao", "पिवट टेबल बनाओ"]),

        (("data_plot",), ["plot the data", "graph banao", "ग्राफ बनाओ"]),

        (("data_compare",), ["compare these two files", "dono files me antar batao",
                            "दो फ़ाइलों की तुलना करो"]),

        (("data_outliers",), ["find outliers", "outliers dhoondho", "असंगत मान निकालो"]),

        (("data_merge",), ["merge these datasets", "datasets jodo", "डेटा मिलाओ"]),

        (("data_query",), ["run this sql query", "database se nikalo",
                          "एसक्यूएल चलाओ"]),

        (("data_export",), ["export to excel", "export as json", "excel me nikalo",
                           "एक्सेल में निकालो"]),

        (("data_dashboard",), ["build a dashboard", "dashboard banao", "डैशबोर्ड बनाओ"]),

        (("data_forecast",), ["forecast next month", "trend batao", "अनुमान लगाओ"]),

    ],
    18: [
        (("search_everything",), ["search everything", "find that thing i saw",
                                 "har jagah dhoondho", "सब जगह ढूंढो"]),

        (("search_documents",), ["find that pdf about taxes", "where is the pdf about",
                                "taxes wali pdf dhoondho", "टैक्स वाली पीडीएफ़ ढूंढो"]),

        (("knowledge_index",), ["index this folder", "folder index karo", "फ़ोल्डर इंडेक्स करो"]),

        (("knowledge_search",), ["search by meaning", "semantic search",
                                "matlab se dhoondho", "अर्थ से खोजो"]),

        (("knowledge_chat",), ["ask these documents", "chat with my notes",
                              "documents se poochho", "दस्तावेज़ों से पूछो"]),

        (("knowledge_recall",), ["what did i do yesterday", "find that thing i saw last week",
                                "kal maine kya kiya", "कल मैंने क्या किया",
                                "पिछले हफ्ते क्या किया था"]),

    ],
    19: [
        (("note_write",), ["write a note", "make a note", "note likho", "नोट लिखो"]),

        (("note_list",), ["show my notes", "search my notes", "notes dikhao", "नोट्स दिखाओ"]),

        (("todo_add",), ["add a todo", "remind me to do", "todo add karo", "कार्य जोड़ो"]),

        (("todo_list",), ["what are my todos", "show my tasks", "todo list dikhao",
                         "कार्य सूची दिखाओ"]),

        (("todo_complete",), ["mark it done", "tick off the todo", "complete karo",
                             "पूरा कर दिया"]),

        (("timer",), ["set a timer", "pomodoro", "start a stopwatch", "alarm lagao",
                     "टाइमर लगाओ", "पोमोडोरो शुरू करो"]),

        (("plan_day",), ["plan my day", "daily plan banao", "दिन की योजना बनाओ"]),

        (("plan_week",), ["weekly review", "week ka review karo", "साप्ताहिक समीक्षा"]),

        (("habits",), ["track my habit", "habit tracker", "aadat track karo", "आदत ट्रैक करो"]),

        (("journal",), ["write in my journal", "journal likho", "जर्नल लिखो"]),

        (("sticky_note",), ["sticky note", "note chipka do", "स्टिकी नोट लगाओ"]),

        (("time_tracking",), ["track my time", "time tracking shuru karo", "समय ट्रैक करो"]),

        (("project_board",), ["update the board", "project board me move karo",
                             "बोर्ड अपडेट करो"]),

        (("focus_session",), ["start a focus session", "focus mode on",
                             "focus shuru karo", "फ़ोकस मोड शुरू करो"]),

    ],
    20: [
        (("learn_flashcards",), ["make flashcards", "quiz me on this", "flashcard banao",
                                "फ़्लैशकार्ड बनाओ"]),

        (("learn_summarize",), ["summarise this chapter", "chapter ka saraansh",
                               "अध्याय का सारांश"]),

        (("learn_explain_simple",), ["explain this simply", "simple tarike se samjhao",
                                    "आसान भाषा में समझाओ"]),

        (("learn_schedule",), ["make a study schedule", "padhai ka schedule banao",
                              "पढ़ाई का समय बनाओ"]),

        (("learn_read_aloud",), ["read this book aloud", "padh ke sunao", "पढ़कर सुनाओ"]),

        (("learn_lecture_notes",), ["lecture notes banao", "व्याख्यान नोट्स"]),

        (("learn_maths",), ["solve this equation", "math problem solve karo",
                           "गणित हल करो"]),

        (("learn_practice",), ["practise english", "learn spanish",
                              "अंग्रेज़ी अभ्यास करो"]),

    ],
    21: [
        (("know_answer",), ["what is", "explain this error", "answer my question",
                           "tell me about", "kya hai ye", "यह क्या है"]),

        (("know_explain",), ["explain in simple words", "explain this", "samjhao",
                             "समझाओ"]),

        (("know_define",), ["what does this mean", "meaning of", "define this word",
                           "iska matlab kya hai", "इसका मतलब क्या है"]),

        (("know_calculate",), ["calculate", "how much is", "work out", "kitna hota hai",
                              "गणना करो", "यूनिट बदलो"]),

        (("know_translate",), ["translate this to hindi", "translate", "hindi me kar do",
                              "अंग्रेज़ी से हिंदी में अनुवाद"]),

        (("know_weather",), ["what is the weather", "weather in", "mausam kaisa hai",
                             "मौसम कैसा है"]),

        (("know_news",), ["what is in the news", "news about", "aaj ki khabrein",
                         "आज की ख़बरें"]),

        (("know_directions",), ["how far is it", "directions to", "route from",
                               "rasta batao", "रास्ता बताओ"]),

        (("know_summarize_url",), ["summarise this page", "summarize this",
                                  "page ka saraansh", "पेज का सारांश"]),

        (("know_currency",), ["convert dollars to rupees", "exchange rate", "usd se inr",
                             "डॉलर से रुपये"]),

        (("know_read_aloud",), ["read this aloud", "read it out loud", "say this out loud",
                                "bol ke sunao", "यह बोलकर सुनाओ"]),

    ],
    22: [
        (("print_file",), ["print this", "print two copies", "print double sided",
                          "print karo", "प्रिंट करो"]),

        (("print_to_pdf",), ["print to pdf", "print this to pdf", "print as pdf",
                             "save as pdf", "pdf me print karo", "पीडीएफ़ में प्रिंट करो",
                             "इसे pdf में बदलो"]),

        (("scan_document",), ["scan this", "document scan karo", "स्कैन करो"]),

        (("print_cancel",), ["cancel the print job", "printing ruk do", "प्रिंट रद्द करो"]),

        (("print_add",), ["add a printer", "printer add karo", "प्रिंटर जोड़ो"]),

        (("print_list",), ["what printers do i have", "printer status batao",
                          "प्रिंटर सूची"]),

    ],
    23: [
        (("cloud_status",), ["is onedrive syncing", "sync status", "क्लाउड सिंक"]),

        (("cloud_sync",), ["sync my folder", "onedrive sync karo", "सिंक करो"]),

        (("cloud_upload",), ["upload to onedrive", "cloud me bhejo", "क्लाउड पर अपलोड करो"]),

        (("cloud_download",), ["download from cloud", "cloud se lao", "क्लाउड से डाउनलोड करो"]),

        (("cloud_share",), ["create a share link", "share link banao", "शेयर लिंक बनाओ"]),

        (("cloud_backup",), ["back up my folder", "backup banao", "बैकअप लो"]),

        (("cloud_free_space",), ["free up space", "disk khaali karo", "जगह खाली करो"]),

        (("cloud_restore",), ["restore the previous version", "purani version lao",
                             "पुरानी वर्शन वापस लाओ"]),

    ],
    24: [
        (("phone_send_file",), ["send this file to my phone", "phone pe bhejo",
                               "फ़ोन पर भेजो"]),

        (("phone_notifications",), ["phone ki notifications", "phone notifications dikhao",
                                   "फ़ोन की सूचनाएँ"]),

        (("phone_reply",), ["reply on my phone", "phone pe reply karo", "फ़ोन पर जवाब दो"]),

        (("phone_cast",), ["cast to my tv", "tv pe bhejo", "टीवी पर भेजो"]),

        (("phone_find",), ["find my phone", "mera phone dhoondho", "फ़ोन ढूंढो"]),

    ],
    25: [
        (("iot_discover",), ["find my smart devices", "devices on network",
                            "स्मार्ट डिवाइस ढूंढो"]),

        (("iot_control",), ["turn on the light", "switch off the plug",
                           "light on karo", "बत्ती जलाओ", "प्लग बंद करो"]),

        (("iot_scene",), ["movie night scene", "good morning scene", "scene chalao",
                         "सीन चलाओ"]),

    ],
    26: [
        (("access_screen_reader",), ["turn on narrator", "screen reader on",
                                    "magnifier on", "larger text", "high contrast",
                                    "narrator chalu karo", "स्क्रीन रीडर चालू करो",
                                    "बड़े अक्षर करो"]),

        (("access_read_screen",), ["read my screen aloud", "screen padh ke sunao",
                                  "स्क्रीन पढ़कर सुनाओ"]),

    ],
    27: [
        (("remote_desktop",), ["connect to my office pc", "remote desktop kholo",
                              "ऑफ़िस पीसी से जुड़ो"]),

        (("remote_ssh",), ["ssh to the server", "server se jodo"]),

        (("remote_vm",), ["start the virtual machine", "vm chalu karo",
                         "वर्चुअल मशीन चालू करो"]),

        (("remote_transfer",), ["copy the file to the server",
                               "server pe file bhejo", "सर्वर पर फ़ाइल भेजो"]),

    ],
    28: [
        (("backup_restore_point",), ["create a restore point", "restore point banao",
                                    "रिस्टोर पॉइंट बनाओ"]),

        (("backup_create",), ["back up my documents", "backup banao", "बैकअप लो"]),

        (("backup_schedule",), ["schedule a daily backup", "roz ka backup",
                               "रोज़ का बैकअप"]),

        (("backup_verify",), ["check the backup", "backup verify karo", "बैकअप जाँचो"]),

        (("backup_restore",), ["restore from backup", "backup se wapas lao",
                              "बैकअप से बहाल करो"]),

        (("backup_recover_file",), ["recover a deleted file", "delete hui file wapas lo",
                                   "हटाई गई फ़ाइल वापस लाओ"]),

        (("backup_history",), ["file history", "previous versions", "पुरानी वर्शन"]),

        (("backup_encrypt",), ["turn on bitlocker", "bitlocker on karo",
                              "बिटलॉकर चालू करो"]),

    ],
    29: [
        (("sec_lock",), ["lock my screen", "lock the computer", "sign out",
                        "screen lock karo", "स्क्रीन लॉक करो", "साइन आउट करो"]),

        (("sec_scan",), ["run a virus scan", "antivirus scan chalao", "वायरस स्कैन करो"]),

        (("sec_firewall",), ["turn the firewall on", "firewall off karo",
                            "फ़ायरवॉल चालू करो"]),

        (("sec_audit_passwords",), ["have my passwords been leaked",
                                   "check password breach", "पासवर्ड लीक हुआ है क्या"]),

        (("sec_password_vault",), ["show my saved passwords", "password manager kholo",
                                  "पासवर्ड मैनेजर"]),

        (("sec_secure_delete",), ["securely delete", "delete for good",
                                 "permanent delete karo", "पूरी तरह मिटा दो"]),

        (("sec_disable_device",), ["disable the webcam", "turn off the microphone",
                                  "camera band karo", "कैमरा बंद करो"]),

        (("sec_permissions",), ["what can this app access", "app permissions dikhao",
                               "ऐप अनुमतियाँ दिखाओ"]),

        (("sec_login_attempts",), ["show login attempts", "kis kis ne login kiya",
                                  "लॉगिन प्रयास दिखाओ"]),

        (("sec_remote_access",), ["disable remote desktop", "remote access off karo",
                                 "रिमोट एक्सेस बंद करो"]),

    ],
    30: [
        (("user_list",), ["who is logged in", "list users", "kaun kaun hai",
                         "कौन-कौन यूज़र हैं"]),

        (("user_switch",), ["switch user", "doosre user pe jao", "यूज़र बदलो"]),

        (("user_add",), ["add a user account", "naya user banao", "नया यूज़र बनाओ"]),

        (("user_remove",), ["remove a user", "user hata do", "यूज़र हटाओ"]),

        (("user_guest",), ["turn on guest mode", "guest mode on", "गेस्ट मोड चालू करो"]),

        (("user_parental",), ["set parental controls", "parental controls on",
                              "पैरेंटल कंट्रोल"]),

        (("user_screen_time",), ["set a screen time limit", "screen time limit",
                                "स्क्रीन टाइम सीमा"]),

        (("user_block_app",), ["block this app for my kid", "app block karo user ke liye"]),

        (("user_kiosk",), ["kiosk mode", "kiosk mode on karo", "कियोस्क मोड"]),

    ],
    31: [
        (("fin_balance",), ["check my bank balance", "kitna paisa hai",
                           "बैंक बैलेंस देखो"]),

        (("fin_statement",), ["download my statement", "statement nikalo", "स्टेटमेंट निकालो"]),

        (("fin_pay",), ["pay my electricity bill", "bill bhar do", "बिल भर दो"]),

        (("fin_expenses",), ["track my expenses", "kharcha track karo", "खर्च ट्रैक करो"]),

        (("fin_categorise",), ["categorise my transactions", "transactions classify karo"]),

        (("fin_invoice",), ["create an invoice", "invoice banao", "इनवॉइस बनाओ"]),

        (("fin_reminder",), ["remind me to pay rent", "payment yaad dila do",
                            "भुगतान की याद दिलाओ"]),

    ],
    32: [
        (("travel_book",), ["book a flight", "book a hotel", "book a cab",
                           "flight book karo", "होटल बुक करो", "फ़्लाइट बुक करो"]),

        (("travel_status",), ["is my flight on time", "flight status batao",
                             "फ़्लाइट स्टेटस"]),

        (("travel_boarding_pass",), ["boarding pass download", "boarding pass dikhao",
                                    "बोर्डिंग पास दिखाओ"]),

        (("travel_itinerary",), ["plan my trip", "make an itinerary",
                                "trip plan banao", "यात्रा की योजना बनाओ"]),

        (("travel_parcel",), ["where is my parcel", "track the package",
                             "पैकेज कहाँ है"]),

        (("travel_subscription",), ["cancel my subscription", "renew netflix",
                                   "subscription cancel karo", "सब्सक्रिप्शन रद्द करो"]),

        (("travel_insurance",), ["compare travel insurance", "bima compare karo",
                                "बीमा तुलना करो"]),

        (("travel_form",), ["fill this government form", "sarkari form bhar do",
                           "सरकारी फ़ॉर्म भर दो"]),

    ],
    33: [
        (("shop_compare",), ["compare these products", "best deal dhoondho",
                            "सबसे सस्ता देखो"]),

        (("shop_price_track",), ["tell me when the price drops", "price track karo",
                                "भाव गिरने पर बताओ"]),

        (("shop_wishlist",), ["add to my wishlist", "wishlist me daalo", "इच्छा सूची"]),

        (("shop_coupon",), ["find a discount code", "coupon code dhoondho",
                           "कूपन ढूंढो"]),

        (("shop_reorder",), ["reorder my last purchase", "dobara order karo",
                            "फिर से ऑर्डर करो"]),

        (("shop_reviews",), ["summarise the reviews", "reviews ka saraansh",
                            "समीक्षाओं का सारांश"]),

        (("shop_delivery",), ["where is my order", "order kahan hai", "ऑर्डर कहाँ है"]),

        (("shop_return",), ["return this item", "refund lo", "वापसी करो"]),

    ],
    34: [
        (("social_post",), ["draft an instagram post", "post likh do",
                           "इंस्टाग्राम पोस्ट लिखो"]),

        (("social_schedule",), ["schedule the post", "post schedule karo",
                               "पोस्ट शेड्यूल करो"]),

        (("social_reply",), ["reply to the comments", "comments ka reply likho",
                            "कमेंट का जवाब लिखो"]),

        (("social_captions",), ["write a caption", "hashtags suggest karo",
                               "कैप्शन लिखो"]),

        (("social_crosspost",), ["post this everywhere", "sab jagah post karo",
                                "हर जगह पोस्ट करो"]),

        (("social_export",), ["download my data", "mera data nikaal do",
                             "मेरा डेटा डाउनलोड करो"]),

    ],
    35: [
        (("game_launch",), ["open steam", "launch the game", "game kholo", "गेम खोलो"]),

        (("game_graphics",), ["set graphics to ultra", "graphics settings badlo",
                             "ग्राफ़िक्स सेटिंग बदलो"]),

        (("game_stream",), ["start streaming", "stream shuru karo", "स्ट्रीम शुरू करो"]),

        (("game_record",), ["record my gameplay", "gameplay record karo",
                           "गेमप्ले रिकॉर्ड करो"]),

        (("game_library",), ["update my games", "games update karo", "गेम्स अपडेट करो"]),

    ],
    36: [
        (("adv_registry",), ["edit the registry", "registry value badlo",
                            "रीजिस्ट्री बदलो"]),

        (("adv_env",), ["set an environment variable", "env variable set karo",
                       "एनवायरनमेंट वैरिएबल सेट करो"]),

        (("adv_hosts",), ["edit the hosts file", "hosts file badlo", "होस्ट्स फ़ाइल"]),

        (("adv_services",), ["list the services", "service stop karo", "सर्विस रोको"]),

        (("adv_task_scheduler",), ["task scheduler", "scheduled tasks dikhao",
                                  "टास्क शेड्यूलर"]),

        (("adv_event_viewer",), ["open event viewer", "event viewer kholo",
                                "इवेंट व्यूअर खोलो"]),

        (("adv_group_policy",), ["group policy report", "gpresult", "ग्रुप पॉलिसी"]),

        (("adv_bios",), ["show bios information", "firmware version", "बायोस जानकारी"]),

        (("adv_boot_order",), ["change the boot order", "boot order badlo",
                              "बूट ऑर्डर बदलो"]),

        (("adv_partition",), ["show my disks", "partitions dikhao", "डिस्क दिखाओ"]),

        (("adv_format",), ["format this drive", "drive format karo", "ड्राइव फ़ॉर्मैट करो"]),

        (("adv_bootable_usb",), ["make a bootable usb", "bootable usb banao",
                                "बूटेबल यूएसबी बनाओ"]),

    ],
    37: [
        (("auto_schedule",), ["schedule this for tomorrow", "run this every morning",
                              "roz chalao", "कल के लिए शेड्यूल करो", "रोज़ चलाओ"]),

        (("auto_cancel",), ["cancel that schedule", "scheduled task ruk do",
                           "शेड्यूल रद्द करो"]),

        (("auto_startup",), ["run this on startup", "startup pe chalao",
                            "स्टार्टअप पर चलाओ"]),

        (("auto_watch_folder",), ["watch this folder and when a pdf appears do x",
                                 "folder watch karo", "फ़ोल्डर देखो"]),

        (("auto_trigger",), ["when this happens do that", "event pe trigger karo",
                            "घटना पर चलाओ"]),

        (("auto_rules",), ["if the battery is low then tell me",
                          "agar battery low ho to batao", "अगर बैटरी कम हो तो बताओ"]),

        (("auto_repeat",), ["do this every 30 minutes", "repeat every hour",
                           "हर घंटे दोहराओ"]),

        (("auto_macro",), ["record what i do and replay it", "macro banao", "मैक्रो बनाओ"]),

        (("auto_background",), ["run this in the background", "background me chalao",
                               "बैकग्राउंड में चलाओ"]),

    ],
    38: [
        (("alert_set",), ["alert me when the download finishes",
                         "notify me when cpu is high", "battery low hone pe batao",
                         "जब डाउनलोड पूरा हो तो बताओ", "सीपीयू ज़्यादा हो तो बताओ"]),

        (("alert_list",), ["show my alerts", "meri alerts dikhao", "अलर्ट दिखाओ"]),

    ],
    39: [
        (("trigger_context",), [
            "before my meeting", "when i get home", "when on my home wifi",
            "at night only", "while i'm away", "when the charger is plugged in",
            "when the screen is locked",
            "meeting se pehle", "ghar aate hi", "raat ko hi", "jab charger laga ho",
            "मेरी मीटिंग से पहले", "रात में ही करो", "चार्जर लगने पर",
        ]),

    ],
    40: [
        (("routine",), [
            "start work setup", "morning routine", "focus mode", "meeting mode",
            "presentation mode", "study mode", "gaming mode", "travel mode",
            "end of day shutdown",
            "kaam shuru karo", "subah ka routine", "shaam ka shutdown",
            "काम शुरू करो", "सुबह की दिनचर्या", "रात को बंद करो",
        ]),

    ],
    41: [
        (("chain",), ["do this then that", "pehle ye phir wo",
                     "इसके बाद वह करो"]),

        (("chain_parallel",), ["do this and that at the same time",
                              "ek saath karo", "एक साथ करो"]),

        (("chain_until",), ["keep doing this until it works", "repeat until done",
                           "जब तक न हो तब तक दोहराओ"]),

        (("chain_foreach",), ["for each file do this", "har file ke liye ye karo",
                             "हर फ़ाइल के लिए"]),

    ],
    42: [
        (("meta_interpret",), [
            "make my laptop faster", "clean up my desktop",
            "get me ready for my meeting", "fix my wifi", "organize my life",
            "find that thing i saw last week", "do what i did yesterday",
            "mere laptop ko tez karo", "mera wifi theek karo",
            "meri meeting ke liye taiyari karo", "mera desktop saaf karo",
            "मेरे लैपटॉप को तेज़ करो", "मेरा वाईफ़ ठीक करो", "मेरा डेस्कटॉप साफ़ करो",
        ]),

    ],
    44: [
        (("ctl_stop",), ["stop", "cancel that", "never mind", "रुको", "बंद करो"]),

        (("ctl_status",), ["what are you doing", "are you still running",
                          "status batao", "तुम क्या कर रहे हो"]),

        (("ctl_history",), ["what did you just do", "explain that",
                           "explain last action", "अभी क्या किया"]),

        (("ctl_retry",), ["do it again", "try that again", "no the other one",
                         "wapas karo", "फिर से करो"]),

        (("ctl_capabilities",), ["what can you do", "show me your tools",
                                "list your skills", "तुम क्या कर सकते हो"]),

        (("ctl_remember",), ["remember this", "forget that", "yaad rakh lo",
                            "याद रखो"]),

    ],
    45: [
        (("safe_mode_policy",), [
            "just show me, don't do it", "ask me before every step",
            "read only mode", "never touch my documents folder",
            "work only in this folder", "dry run first",
            "sirf dikhao karna hai", "har step pe puchho", "केवल दिखाओ",
        ]),

        (("safe_always_approve",), ["stop asking me about safe stuff",
                                   "har baar mat puchho", "बार-बार मत पूछो"]),

    ],
    46: [
        (("feedback_correct",), [
            "that was wrong", "you missed one", "do it like last time",
            "always do it this way", "never do that again", "why did you do that",
            "show me before applying",
            "ye galat tha", "ek chhoot gaya", "hamesha aise karo", "galat kiya",
        ]),

    ],
    47: [
        (("meta_preferences",), ["change your settings", "show your preferences",
                                "set your preferences", "अपनी सेटिंग्स बदलो"]),

        (("meta_teach",), ["teach yourself to do this", "learn this task",
                          "ye seekh lo", "यह सीख लो"]),

        (("meta_workflow",), ["save this as a command", "list my workflows",
                             "export your workflows", "इसे कमांड बनाकर सेव करो"]),

        (("meta_model",), ["switch to another model", "which model are you using",
                          "मॉडल बदलो"]),

        (("meta_stats",), ["how much have you used", "show your usage",
                          "errors dikhao", "उपयोग दिखाओ"]),

        (("meta_reset",), ["reset everything to defaults", "defaults pe wapas jao",
                          "सब कुछ रीसेट करो"]),

        (("meta_update",), ["update yourself", "update ufo", "upgrade the agent",
                           "get the latest version", "khud ko update karo",
                           "अपने आप को अपडेट करो"]),

        (("meta_permissions",), ["set permissions per category", "permissions for finance",
                                 "block a whole category", "allow the whole category",
                                 "ask before this category", "category permissions"]),

    ],
    48: [
        (("meta_gap",), ["you cannot do this", "that is not supported",
                        "ye nahi kar sakte", "यह नहीं हो सका"]),

    ],
}

# --------------------------------------------------------------------------
# vague instructions -> a concrete list of capabilities
# --------------------------------------------------------------------------
RECIPES: list[tuple[tuple[str, ...], list[str]]] = [
    (("make my laptop faster", "speed up my pc", "computer tez karo",
      "मेरा लैपटॉप तेज़ करो", "कंप्यूटर तेज़ कर दो", "pc slow hai", "slow computer",
      "make it faster", "speed it up", "make this faster", "my pc is slow",
      "computer slow kyun hai", "बहुत धीमा है"),
     ["fix_speed_up", "hw_status", "set_power_plan", "app_startup_list",
      "hw_storage_report"]),
    (("clean up my desktop", "tidy my desktop", "desktop saaf karo",
      "मेरा डेस्कटॉप साफ़ करो"),
     ["file_organise", "file_find"]),
    (("get me ready for my meeting", "meeting ke liye taiyar karo",
      "prep for my meeting", "मेरी मीटिंग के लिए तैयारी करो"),
     ["routine:meeting", "calendar_events", "notifications", "set_do_not_disturb:on",
      "set_volume:50", "meeting_control:mute"]),
    (("fix my wifi", "wifi theek karo", "wifi kaam nahi kar raha",
      "मेरा वाईफ़ ठीक करो"),
     ["net_profile", "net_wifi_list", "fix_reset_network", "net_flush_dns",
      "net_fix"]),
    (("organize my life", "meri life organize karo", "जीवन व्यवस्थित करो"),
     ["plan_day", "todo_list", "plan_week", "journal"]),
    (("find that thing i saw last week", "find that thing",
      "kya dekha tha pichle hafte", "पिछले हफ्ते क्या देखा था"),
     ["knowledge_recall:last_week", "search_everything", "search_documents"]),
    (("do what i did yesterday", "kal jaisa karo", "yesterday wala kaam do",
      "कल जैसा काम करो"),
     ["knowledge_recall:yesterday", "ctl_history", "ctl_retry"]),
    (("clean my downloads", "downloads saaf karo", "डाउनलोड साफ़ करो"),
     ["file_organise:downloads", "file_organise:duplicates", "file_organise:large"]),
    (("back up my stuff", "backup banao", "बैकअप लो"),
     ["cloud_backup", "backup_schedule", "backup_verify"]),
    (("is everything ok", "pc theek hai", "सब ठीक है?"),
     ["hw_status", "hw_battery", "fix_diagnose", "net_profile"]),
    (("prepare for tomorrow", "kal ke liye tayyar karo", "कल के लिए तैयार करो"),
     ["plan_day", "todo_list", "travel_boarding_pass", "notifications"]),
    (("tidy my photos", "photos saaf karo", "तस्वीरें साफ़ करो"),
     ["file_organise:by_date", "file_organise:duplicates", "image_batch"]),
    (("reduce my screen time", "screen time kam karo", "स्क्रीन टाइम कम करो"),
     ["user_screen_time", "user_parental", "todo_list", "plan_week"]),
    (("set up my new laptop", "new laptop setup", "नया लैपटॉप सेट अप करो"),
     ["set_power_plan", "app_startup_list", "set_brightness", "set_theme",
      "windows_update:check", "hw_system_info"]),
    (("i am busy", "i am in a meeting", "i am travelling", "i am away",
      "main busy hoon", "मैं व्यस्त हूं"),
     ["routine:focus", "set_do_not_disturb:on", "alert_set"]),
    (("i am back", "i am home", "main aa gaya", "मैं आ गया"),
     ["routine:work", "set_do_not_disturb:off", "net_profile"]),
    (("good morning", "subah ka routine", "सुबह का नमस्ते"),
     ["routine:morning", "net_profile", "notifications", "plan_day", "hw_battery"]),
    (("good night", "end of day", "end of day shutdown", "raat ho gayi",
      "दिन समाप्त"),
     ["routine:end_of_day", "backup_schedule", "set_power:lock"]),
]

