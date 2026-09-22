-- Priority Harmonization Engine — SQL Server / Fabric warehouse landing.
-- Apply after IW29 is staged (csv or table). Priority_Final is the only
-- field operational visuals should bind to. SAPPriority is audit-only.
--
-- Rank convention: 1 = most severe. Intermediate (SAP matrix) = 3.

CREATE OR ALTER VIEW dbo.v_PriorityMapping AS
SELECT * FROM (VALUES
    (1, N'1',          N'Immediate'),
    (1, N'01',         N'Very high'),
    (1, N'Immediate',  N'Immediate'),
    (1, N'Very high',  N'Very high'),
    (1, N'Emergency',  N'Emergency'),
    (1, N'Critical',   N'Critical'),
    (2, N'2',          N'Urgent'),
    (2, N'02',         N'High'),
    (2, N'Urgent',     N'Urgent'),
    (2, N'High',       N'High'),
    (3, N'3',          N'Intermediate'),
    (3, N'03',         N'Medium'),
    (3, N'Intermediate', N'Intermediate'),
    (3, N'Medium',     N'Medium'),
    (3, N'Normal',     N'Normal'),
    (4, N'4',          N'Low'),
    (4, N'04',         N'Low'),
    (4, N'Low',        N'Low'),
    (5, N'5',          N'Very low'),
    (5, N'05',         N'Planning'),
    (5, N'Very low',   N'Very low'),
    (5, N'Planning',   N'Planning')
) AS m (Rank, SAPPriority, Label);
GO

CREATE OR ALTER VIEW dbo.v_NotificationsHarmonized AS
WITH src AS (
    SELECT
        CAST(Notification AS nvarchar(20))              AS NotificationNumber,
        CAST([Description] AS nvarchar(255))            AS [Description],
        CAST([Functional Location] AS nvarchar(40))     AS FunctionalLocation,
        CAST(Equipment AS nvarchar(18))                 AS Equipment,
        CAST(Priority AS nvarchar(40))                  AS SAPPriority,
        CAST([Created On] AS date)                      AS NotificationDate,
        CAST([Required End] AS date)                    AS RequiredEndDate,
        CAST([System Status] AS nvarchar(40))           AS NotificationStatus
    FROM dbo.IW29_Notifications
),
parsed AS (
    SELECT
        s.*,
        NULLIF(LTRIM(RTRIM(s.[Description])), N'') AS DescriptionClean,
        RIGHT(
            REPLACE(REPLACE(LTRIM(RTRIM(s.[Description])), N'\', N'/'), N' ', N''),
            CHARINDEX(
                N'/',
                REVERSE(REPLACE(REPLACE(LTRIM(RTRIM(s.[Description])), N'\', N'/'), N' ', N'')) + N'/'
            ) - 1
        ) AS LastToken
    FROM src s
),
ranked AS (
    SELECT
        p.*,
        TRY_CONVERT(int, p.LastToken) AS InspectorPriorityRaw,
        m.Rank AS SAPPriorityRank
    FROM parsed p
    LEFT JOIN dbo.v_PriorityMapping m
        ON LOWER(p.SAPPriority) = LOWER(m.SAPPriority)
)
SELECT
    NotificationNumber,
    [Description],
    FunctionalLocation,
    Equipment,
    SAPPriority,
    SAPPriorityRank,
    CASE
        WHEN InspectorPriorityRaw BETWEEN 1 AND 5 THEN InspectorPriorityRaw
    END                                                     AS InspectorPriority,
    CASE
        WHEN InspectorPriorityRaw BETWEEN 1 AND 5 THEN InspectorPriorityRaw
    END                                                     AS Priority_Final,
    CASE
        WHEN InspectorPriorityRaw BETWEEN 1 AND 5
         AND SAPPriorityRank IS NOT NULL
         AND InspectorPriorityRaw <> SAPPriorityRank
            THEN N'Yes'
        WHEN InspectorPriorityRaw BETWEEN 1 AND 5
         AND SAPPriorityRank IS NOT NULL
            THEN N'No'
    END                                                     AS PriorityMismatch,
    CASE
        WHEN InspectorPriorityRaw BETWEEN 1 AND 5
         AND SAPPriorityRank IS NOT NULL
            THEN InspectorPriorityRaw - SAPPriorityRank
    END                                                     AS PriorityVariance,
    CASE
        WHEN InspectorPriorityRaw BETWEEN 1 AND 5
            THEN N'Inspection Naming Convention'
        ELSE N'Unparsed'
    END                                                     AS PrioritySource,
    CASE
        WHEN InspectorPriorityRaw BETWEEN 1 AND 5
         AND LEN(DescriptionClean) - LEN(REPLACE(DescriptionClean, N'/', N'')) >= 5
            THEN N'High'
        WHEN InspectorPriorityRaw BETWEEN 1 AND 5
            THEN N'Medium'
        WHEN SAPPriorityRank IS NOT NULL
            THEN N'Low'
        ELSE N'None'
    END                                                     AS PriorityConfidence,
    CASE
        WHEN [Description] LIKE N'%/FZ[0-9]%'
            THEN N'FZ' + SUBSTRING(
                [Description],
                PATINDEX(N'%/FZ[0-9]%', [Description]) + 3,
                PATINDEX(
                    N'%[^0-9]%',
                    SUBSTRING([Description], PATINDEX(N'%/FZ[0-9]%', [Description]) + 3, 8) + N'/'
                ) - 1
            )
    END                                                     AS FireZone,
    NotificationDate,
    RequiredEndDate,
    NotificationStatus
FROM ranked;
GO

CREATE OR ALTER VIEW dbo.v_PriorityVariance AS
SELECT
    NotificationNumber,
    SAPPriority,
    InspectorPriority,
    PriorityVariance AS Variance
FROM dbo.v_NotificationsHarmonized
WHERE PriorityMismatch = N'Yes';
GO

-- KPI reconciliation
-- PriorityAccuracyPct   = 1 - mismatches / parsed
-- PriorityVariancePct   = mismatches / parsed
-- MisclassifiedCount    = COUNT where PriorityMismatch = 'Yes'
-- SapUnderRankedCount   = COUNT where PriorityVariance < 0
--   (inspector more severe than SAP — potential risk exposure)
