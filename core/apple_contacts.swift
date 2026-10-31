// Contacts.framework helper for `comms contact` (contactbook.py builds and runs it).
// Commands: status, find, add, update, delete, search, list. Prints one JSON object.
// Exit: 0 ok, 2 bad call, 4 card does not match, 5 no access, 6 store error.
//
// It never asks for access. A store call while access is undecided opens a macOS dialog, and a
// launchd job has no terminal grant, so every command stops at the status check instead.
import Contacts
import Foundation

func emit(_ object: [String: Any], code: Int32 = 0) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    exit(code)
}

let args = Array(CommandLine.arguments.dropFirst())

func values(_ flag: String) -> [String] {
    var out: [String] = []
    for (index, arg) in args.enumerated() where arg == flag && index + 1 < args.count {
        out.append(args[index + 1])
    }
    return out
}

func fullName(_ contact: CNContact) -> String {
    return "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces)
}

func digits(_ text: String) -> String {
    return text.filter { $0.isNumber }
}

// A US number written with or without its leading 1 is the same number.
func phoneDigits(_ text: String) -> String {
    let d = digits(text)
    return d.count == 11 && d.hasPrefix("1") ? String(d.dropFirst()) : d
}

func samePhone(_ a: String, _ b: String) -> Bool {
    let x = phoneDigits(a)
    return !x.isEmpty && x == phoneDigits(b)
}

func sameEmail(_ a: String, _ b: String) -> Bool {
    return a.trimmingCharacters(in: .whitespaces).caseInsensitiveCompare(b.trimmingCharacters(in: .whitespaces)) == .orderedSame
}

func displayName(_ contact: CNContact) -> String {
    let name = fullName(contact)
    return name.isEmpty ? contact.organizationName : name
}

func row(_ card: CNContact) -> [String: Any] {
    return ["id": card.identifier, "name": displayName(card),
            "phones": card.phoneNumbers.map { $0.value.stringValue },
            "emails": card.emailAddresses.map { $0.value as String }]
}

guard let command = args.first else { emit(["error": "usage"], code: 2) }

let rawAuth = CNContactStore.authorizationStatus(for: .contacts).rawValue
let authNames = [0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized", 4: "limited"]
let auth = authNames[rawAuth] ?? "unknown"
guard rawAuth == 3 else {
    if command == "status" { emit(["auth": auth]) }
    emit(["error": "no_access", "auth": auth], code: 5)
}

let store = CNContactStore()
let keys: [CNKeyDescriptor] = [CNContactGivenNameKey, CNContactFamilyNameKey,
                               CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
                               CNContactOrganizationNameKey] as [CNKeyDescriptor]

// Every card, sorted by name. Reads only; nothing is cached or written.
func allCards() throws -> [CNContact] {
    var cards: [CNContact] = []
    try store.enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) { card, _ in cards.append(card) }
    return cards.sorted { displayName($0).localizedCaseInsensitiveCompare(displayName($1)) == .orderedAscending }
}

do {
    switch command {
    case "status":
        let containers = try store.containers(matching: nil)
        let defaultId = store.defaultContainerIdentifier()
        let defaultName = containers.first { $0.identifier == defaultId }?.name
        // A card outside iCloud stays on this Mac, so the iPhone and WhatsApp never see it.
        let isICloud: (CNContainer) -> Bool = { $0.name.lowercased() == "icloud" && $0.type == .cardDAV }
        let icloud = containers.first { $0.identifier == defaultId && isICloud($0) }
            ?? containers.first(where: isICloud)
        emit(["auth": auth, "default_container_id": defaultId,
              "default_container_name": defaultName ?? NSNull(),
              "icloud_container": icloud?.identifier ?? NSNull()])

    case "find":
        var byHandle: [String: String] = [:]
        for phone in values("--phone") {
            let predicate = CNContact.predicateForContacts(matching: CNPhoneNumber(stringValue: phone))
            for card in try store.unifiedContacts(matching: predicate, keysToFetch: keys) {
                byHandle[card.identifier] = fullName(card)
            }
        }
        for email in values("--email") {
            let predicate = CNContact.predicateForContacts(matchingEmailAddress: email)
            for card in try store.unifiedContacts(matching: predicate, keysToFetch: keys) {
                byHandle[card.identifier] = fullName(card)
            }
        }
        var byName: [[String: String]] = []
        if let name = values("--name").first {
            // matchingName is fuzzy (it hits "Morgan Leeson"), so keep the exact name only.
            let predicate = CNContact.predicateForContacts(matchingName: name)
            for card in try store.unifiedContacts(matching: predicate, keysToFetch: keys)
            where byHandle[card.identifier] == nil
                && fullName(card).caseInsensitiveCompare(name) == .orderedSame {
                byName.append(["id": card.identifier, "name": fullName(card)])
            }
        }
        emit(["by_handle": byHandle.map { ["id": $0.key, "name": $0.value] }, "by_name": byName])

    case "add":
        guard let container = values("--container").first else { emit(["error": "no container"], code: 2) }
        let card = CNMutableContact()
        card.givenName = values("--given").first ?? ""
        card.familyName = values("--family").first ?? ""
        card.phoneNumbers = values("--phone").map {
            CNLabeledValue(label: CNLabelPhoneNumberMobile, value: CNPhoneNumber(stringValue: $0))
        }
        card.emailAddresses = values("--email").map {
            CNLabeledValue(label: CNLabelOther, value: $0 as NSString)
        }
        let request = CNSaveRequest()
        request.add(card, toContainerWithIdentifier: container)
        try store.execute(request)
        emit(["id": card.identifier])

    case "update":
        // Changes only what is named. Every other number, email, note, photo or birthday on the
        // card is left as it is: the card is fetched, edited in place and saved as an update.
        guard let id = values("--id").first else { emit(["error": "update needs --id"], code: 2) }
        let names = values("--expect-name"), handles = values("--expect-handle")
        guard !names.isEmpty || !handles.isEmpty else {
            emit(["error": "update needs --expect-name or --expect-handle"], code: 2)
        }
        let card = try store.unifiedContact(withIdentifier: id, keysToFetch: keys)
        let nameOk = names.contains { fullName(card).caseInsensitiveCompare($0) == .orderedSame }
        let handleOk = handles.contains { h in
            card.phoneNumbers.contains { samePhone($0.value.stringValue, h) }
                || card.emailAddresses.contains { sameEmail($0.value as String, h) }
        }
        guard nameOk || handleOk else {
            emit(["error": "card_name_changed", "name": fullName(card)], code: 4)
        }
        let edit = card.mutableCopy() as! CNMutableContact
        var changes: [String] = []
        if let given = values("--given").first {
            edit.givenName = given
            edit.familyName = values("--family").first ?? ""
            changes.append("name")
        }
        for pair in values("--replace-phone") {
            let parts = pair.components(separatedBy: "=")
            guard parts.count == 2 else { emit(["error": "bad --replace-phone"], code: 2) }
            let (old, new) = (parts[0], parts[1])
            let haveNew = edit.phoneNumbers.contains { samePhone($0.value.stringValue, new) }
            if let at = edit.phoneNumbers.firstIndex(where: { samePhone($0.value.stringValue, old) }) {
                if haveNew && !samePhone(old, new) {
                    edit.phoneNumbers.remove(at: at)
                } else {
                    edit.phoneNumbers[at] = edit.phoneNumbers[at].settingValue(CNPhoneNumber(stringValue: new))
                }
                changes.append("phone")
            } else if !haveNew {
                edit.phoneNumbers.append(CNLabeledValue(label: CNLabelPhoneNumberMobile, value: CNPhoneNumber(stringValue: new)))
                changes.append("phone")
            }
        }
        for phone in values("--add-phone")
        where !edit.phoneNumbers.contains(where: { samePhone($0.value.stringValue, phone) }) {
            edit.phoneNumbers.append(CNLabeledValue(label: CNLabelPhoneNumberMobile, value: CNPhoneNumber(stringValue: phone)))
            changes.append("phone")
        }
        for pair in values("--replace-email") {
            let parts = pair.components(separatedBy: "=")
            guard parts.count == 2 else { emit(["error": "bad --replace-email"], code: 2) }
            let (old, new) = (parts[0], parts[1])
            let haveNew = edit.emailAddresses.contains { sameEmail($0.value as String, new) }
            if let at = edit.emailAddresses.firstIndex(where: { sameEmail($0.value as String, old) }) {
                if haveNew && !sameEmail(old, new) {
                    edit.emailAddresses.remove(at: at)
                } else {
                    edit.emailAddresses[at] = edit.emailAddresses[at].settingValue(new as NSString)
                }
                changes.append("email")
            } else if !haveNew {
                edit.emailAddresses.append(CNLabeledValue(label: CNLabelOther, value: new as NSString))
                changes.append("email")
            }
        }
        for email in values("--add-email")
        where !edit.emailAddresses.contains(where: { sameEmail($0.value as String, email) }) {
            edit.emailAddresses.append(CNLabeledValue(label: CNLabelOther, value: email as NSString))
            changes.append("email")
        }
        if !changes.isEmpty {
            let request = CNSaveRequest()
            request.update(edit)
            try store.execute(request)
        }
        emit(["updated": id, "changes": changes])

    case "search", "list":
        // search --text T matches a name, a number (digits only) or an email. list is every card.
        // --limit caps the rows (0 = all); total is always the true count. Output only.
        let text = (command == "search" ? values("--text").first : nil) ?? ""
        if command == "search" && text.trimmingCharacters(in: .whitespaces).isEmpty {
            emit(["error": "search needs --text"], code: 2)
        }
        let limit = Int(values("--limit").first ?? "") ?? 0
        let query = text.lowercased()
        let wanted = phoneDigits(text)
        let numberLike = text.range(of: "^[0-9+\\-() .]+$", options: .regularExpression) != nil && wanted.count >= 3
        let hits = try allCards().filter { card in
            if command == "list" { return true }
            return displayName(card).lowercased().contains(query)
                || card.emailAddresses.contains { ($0.value as String).lowercased().contains(query) }
                || (numberLike && card.phoneNumbers.contains { phoneDigits($0.value.stringValue).contains(wanted) })
        }
        let shown = limit > 0 ? Array(hits.prefix(limit)) : hits
        emit(["total": hits.count, "rows": shown.map(row)])

    case "delete":
        guard let id = values("--id").first, let expected = values("--expect-name").first else {
            emit(["error": "delete needs --id and --expect-name"], code: 2)
        }
        let card = try store.unifiedContact(withIdentifier: id, keysToFetch: keys)
        guard fullName(card).caseInsensitiveCompare(expected) == .orderedSame else {
            emit(["error": "card_name_changed", "name": fullName(card)], code: 4)
        }
        let request = CNSaveRequest()
        request.delete(card.mutableCopy() as! CNMutableContact)
        try store.execute(request)
        emit(["deleted": id])

    default:
        emit(["error": "unknown command \(command)"], code: 2)
    }
} catch {
    emit(["error": error.localizedDescription], code: 6)
}
