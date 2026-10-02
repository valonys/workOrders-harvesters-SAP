# FPSO_Inspection — slicer "reverts to All" fix

Symptom: picking an item in a slicer does not filter the page; the visual
keeps showing everything.

## 1. Prove it is data-side first (2 min, no PBIX edit)

```powershell
python -m iw29_export filter-check
```

Fix every `ISSUE` line before touching the report. The usual hits are
trailing spaces in `ItemClass`, case splits (`Campaign` vs `campaign`),
blank `ItemClass` rows, or a stale `CLV_wo_fact.csv` mirror.

## 2. Point every slicer at the dimension (5 min)

The fact CSV is flat, so it is easy to slice `Fact[ItemClass]` while the
visual groups by a lookup column (or vice versa). That selection can never
propagate.

- Slicers must use `DimSite[Site]`, `DimItemClass[ItemClass]`,
  `DimStatus[StatusPrimary]` from `pbi/fpso_inspection_filter_fix.pq.m`.
- Never slice a `Fact[...]` column directly.
- Delete any direct `Fact <-> FPSO_item_class_lookup` relationship. The
  lookup CSV is a harvest cache, not a model dimension.

## 3. Set each relationship to single direction (2 min)

Model view, for each `Dim -> Fact` relationship:

- Cardinality: one to many, single cross-filter direction (`Dim` to `Fact`).
- Enable "Assume referential integrity".
- No bidirectional relationships. Bidirectional lets a visual's own filter
  flow back into the slicer table and clear the selection.

## 4. Check the measures (3 min)

Open each measure on the affected visual. Remove `ALL`, `ALLEXCEPT`, or
`REMOVEFILTERS` over any slicer column. Use the replacements in
`pbi/fpso_inspection_filter_fix.dax`, which preserve slicer context and show
`(All)` instead of blank when several items are selected.

## 5. Check visual interactions (1 min)

Select the slicer, open Format > Edit interactions, and confirm the target
visual shows the filter icon (not the crossed-out one). Re-select it if a
previous author disabled it.

## 6. Refresh and publish

```powershell
.\scripts\refresh_fpso_powerbi.ps1
```

Pick one slicer value, confirm the work-order table filters, then check the
`(All)` card from the DAX pack reads the selected item.
