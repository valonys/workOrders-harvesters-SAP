// FPSO_Inspection slicer fix — star-schema Power Query.
// Problem: slicers built on Fact columns (or on a lookup table with no
// relationship) do not propagate, so a selection falls back to "All".
// Fix: build dimensions from the fact with trimmed keys, relate them
// single-direction Dim -> Fact, and point every slicer at the Dim column.
//
// Wiring (Model view):
//   DimSite[Site] 1 -> * Fact[Site]                     (single)
//   DimItemClass[ItemClass] 1 -> * Fact[ItemClass]     (single)
//   DimStatus[StatusPrimary] 1 -> * Fact[StatusPrimary] (single)
// Cross-filter direction: Single. Assume referential integrity: yes.
// Delete any direct Fact <-> FPSO_item_class_lookup relationship; the
// lookup CSV is a harvest cache, not a model dimension.

let
    FactRaw = Csv.Document(
        File.Contents(DatasetFolder & "FPSO_wo_fact.csv"),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    FactPromoted = Table.PromoteHeaders(FactRaw, [PromoteAllScalars = true]),

    // Trim every slicer key once, at the source. Trailing spaces are the
    // most common silent slicer killer ("Campaign " vs "Campaign").
    TrimKeys = Table.TransformColumns(
        FactPromoted,
        List.Transform(
            {"Site", "ItemClass", "StatusPrimary", "SECE", "BacklogBucket"},
            each {_, each try Text.Trim(_) otherwise _, type text}
        ),
        MissingField.Ignore
    ),
    Fact = Table.ReplaceValue(
        TrimKeys, "", null, Replacer.ReplaceValue, {"ItemClass"}, MissingField.Ignore
    ),

    DimSite = Table.Distinct(
        Table.SelectColumns(
            Table.SelectRows(Fact, each [Site] <> null and [Site] <> ""),
            {"Site"}
        )
    ),
    DimSiteKey = Table.AddColumn(
        DimSite, "SiteKey", each Text.Lower([Site]), type text
    ),

    DimItemClassBase = Table.SelectRows(
        Fact, each [ItemClass] <> null and [ItemClass] <> ""
    ),
    DimItemClass = Table.Distinct(
        Table.SelectColumns(DimItemClassBase, {"ItemClass"})
    ),
    // One display casing per key: keeps "Campaign" and "campaign" from
    // splitting the slicer into two items that filter nothing together.
    DimItemClassKey = Table.AddColumn(
        DimItemClass, "ItemClassKey", each Text.Lower([ItemClass]), type text
    ),

    DimStatus = Table.Distinct(
        Table.SelectColumns(
            Table.SelectRows(Fact, each [StatusPrimary] <> null and [StatusPrimary] <> ""),
            {"StatusPrimary"}
        )
    ),

    // Age buckets keep harvest sort order via BucketSort.
    DimBucket = Table.Distinct(
        Table.SelectColumns(
            Table.SelectRows(Fact, each [BacklogBucket] <> null and [BacklogBucket] <> ""),
            {"BacklogBucket", "BucketSort"}
        )
    ),
    DimBucketSorted = Table.Sort(DimBucket, {{"BucketSort", Order.Ascending}})
in
    Fact
