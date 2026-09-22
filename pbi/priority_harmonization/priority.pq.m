// Priority Harmonization Engine — Power Query M
// Use when the Python ETL has not already appended the columns (raw IW29).
// If iw29_dataset.csv already contains Priority_Final, skip this query and
// bind visuals to that column.
//
// Rank convention: 1 = most severe. Intermediate = 3.

let
    Source = Iw29Raw,

    #"Typed" = Table.TransformColumnTypes(
        Source,
        {
            {"Notification", type text},
            {"Description", type text},
            {"Priority", type text},
            {"Functional Location", type text},
            {"Equipment", type text}
        },
        "en-GB"
    ),

    #"SAP Rank Map" = #table(
        {"SAPPriorityKey", "SAPPriorityRank"},
        {
            {"1", 1}, {"01", 1}, {"immediate", 1}, {"very high", 1}, {"emergency", 1}, {"critical", 1},
            {"2", 2}, {"02", 2}, {"urgent", 2}, {"high", 2},
            {"3", 3}, {"03", 3}, {"intermediate", 3}, {"medium", 3}, {"normal", 3},
            {"4", 4}, {"04", 4}, {"low", 4},
            {"5", 5}, {"05", 5}, {"very low", 5}, {"planning", 5}
        }
    ),

    #"With Tokens" = Table.AddColumn(
        #"Typed",
        "NamingTokens",
        each List.Select(
            Text.Split(Text.Replace(Text.Trim([Description] ?? ""), "\", "/"), "/"),
            each _ <> ""
        ),
        type list
    ),

    #"Inspector" = Table.AddColumn(
        #"With Tokens",
        "InspectorPriority",
        each
            let
                last = List.Last([NamingTokens], null),
                digits = if last = null then null
                    else Text.Select(Text.Upper(last), {"0".."9"}),
                n = try Number.FromText(digits) otherwise null
            in
                if n <> null and n >= 1 and n <= 5 then n else null,
        Int64.Type
    ),

    #"SAP Key" = Table.AddColumn(
        #"Inspector",
        "SAPPriorityKey",
        each Text.Lower(Text.Trim([Priority] ?? "")),
        type text
    ),

    #"Joined" = Table.NestedJoin(
        #"SAP Key", {"SAPPriorityKey"},
        #"SAP Rank Map", {"SAPPriorityKey"},
        "map", JoinKind.LeftOuter
    ),
    #"Expanded Map" = Table.ExpandTableColumn(#"Joined", "map", {"SAPPriorityRank"}),

    #"Harmonized" = Table.AddColumn(
        #"Expanded Map",
        "Priority_Final",
        each [InspectorPriority],
        Int64.Type
    ),

    #"Mismatch" = Table.AddColumn(
        #"Harmonized",
        "PriorityMismatch",
        each
            if [InspectorPriority] = null or [SAPPriorityRank] = null then null
            else if [InspectorPriority] <> [SAPPriorityRank] then "Yes"
            else "No",
        type text
    ),

    #"Variance" = Table.AddColumn(
        #"Mismatch",
        "PriorityVariance",
        each
            if [InspectorPriority] = null or [SAPPriorityRank] = null then null
            else [InspectorPriority] - [SAPPriorityRank],
        Int64.Type
    ),

    #"Source Flag" = Table.AddColumn(
        #"Variance",
        "PrioritySource",
        each if [InspectorPriority] <> null then "Inspection Naming Convention" else "Unparsed",
        type text
    ),

    #"Confidence" = Table.AddColumn(
        #"Source Flag",
        "PriorityConfidence",
        each
            if [InspectorPriority] <> null and List.Count([NamingTokens]) >= 6 then "High"
            else if [InspectorPriority] <> null then "Medium"
            else if [SAPPriorityRank] <> null then "Low"
            else "None",
        type text
    ),

    #"Fire Zone" = Table.AddColumn(
        #"Confidence",
        "FireZone",
        each
            let
                hit = List.First(
                    List.Select([NamingTokens], each Text.StartsWith(Text.Upper(_), "FZ")),
                    null
                )
            in
                if hit = null then null else "FZ" & Text.Select(hit, {"0".."9"}),
        type text
    ),

    #"Renamed SAP" = Table.RenameColumns(
        #"Fire Zone",
        {{"Priority", "SAPPriority"}},
        MissingField.Ignore
    ),

    #"Notifications" = Table.SelectColumns(
        #"Renamed SAP",
        {
            "Notification",
            "Description",
            "Functional Location",
            "Equipment",
            "SAPPriority",
            "SAPPriorityRank",
            "InspectorPriority",
            "Priority_Final",
            "PriorityMismatch",
            "PriorityVariance",
            "PrioritySource",
            "PriorityConfidence",
            "FireZone"
        },
        MissingField.Ignore
    ),

    PriorityMapping = #"SAP Rank Map",

    PriorityVariance = Table.SelectRows(
        #"Notifications",
        each [PriorityMismatch] = "Yes"
    )
in
    #"Notifications"
