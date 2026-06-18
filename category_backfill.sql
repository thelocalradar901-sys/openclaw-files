-- OpenClaw category backfill
-- Step 1: Ensure all 7 TLR categories exist as tribe_events_cat terms


INSERT IGNORE INTO wp_terms (name, slug, term_group)
SELECT 'Live Music & Concerts', 'live-music-concerts', 0
WHERE NOT EXISTS (SELECT 1 FROM wp_terms WHERE slug='live-music-concerts');

INSERT IGNORE INTO wp_term_taxonomy (term_id, taxonomy, description, parent, count)
SELECT term_id, 'tribe_events_cat', '', 0, 0
FROM wp_terms WHERE slug='live-music-concerts'
AND NOT EXISTS (
    SELECT 1 FROM wp_term_taxonomy tt
    JOIN wp_terms t ON tt.term_id=t.term_id
    WHERE t.slug='live-music-concerts' AND tt.taxonomy='tribe_events_cat'
);

INSERT IGNORE INTO wp_terms (name, slug, term_group)
SELECT 'Comedy', 'comedy', 0
WHERE NOT EXISTS (SELECT 1 FROM wp_terms WHERE slug='comedy');

INSERT IGNORE INTO wp_term_taxonomy (term_id, taxonomy, description, parent, count)
SELECT term_id, 'tribe_events_cat', '', 0, 0
FROM wp_terms WHERE slug='comedy'
AND NOT EXISTS (
    SELECT 1 FROM wp_term_taxonomy tt
    JOIN wp_terms t ON tt.term_id=t.term_id
    WHERE t.slug='comedy' AND tt.taxonomy='tribe_events_cat'
);

INSERT IGNORE INTO wp_terms (name, slug, term_group)
SELECT 'Performing & Visual Arts', 'performing-visual-arts', 0
WHERE NOT EXISTS (SELECT 1 FROM wp_terms WHERE slug='performing-visual-arts');

INSERT IGNORE INTO wp_term_taxonomy (term_id, taxonomy, description, parent, count)
SELECT term_id, 'tribe_events_cat', '', 0, 0
FROM wp_terms WHERE slug='performing-visual-arts'
AND NOT EXISTS (
    SELECT 1 FROM wp_term_taxonomy tt
    JOIN wp_terms t ON tt.term_id=t.term_id
    WHERE t.slug='performing-visual-arts' AND tt.taxonomy='tribe_events_cat'
);

INSERT IGNORE INTO wp_terms (name, slug, term_group)
SELECT 'Sports & Fitness', 'sports-fitness', 0
WHERE NOT EXISTS (SELECT 1 FROM wp_terms WHERE slug='sports-fitness');

INSERT IGNORE INTO wp_term_taxonomy (term_id, taxonomy, description, parent, count)
SELECT term_id, 'tribe_events_cat', '', 0, 0
FROM wp_terms WHERE slug='sports-fitness'
AND NOT EXISTS (
    SELECT 1 FROM wp_term_taxonomy tt
    JOIN wp_terms t ON tt.term_id=t.term_id
    WHERE t.slug='sports-fitness' AND tt.taxonomy='tribe_events_cat'
);

INSERT IGNORE INTO wp_terms (name, slug, term_group)
SELECT 'Festivals', 'festivals', 0
WHERE NOT EXISTS (SELECT 1 FROM wp_terms WHERE slug='festivals');

INSERT IGNORE INTO wp_term_taxonomy (term_id, taxonomy, description, parent, count)
SELECT term_id, 'tribe_events_cat', '', 0, 0
FROM wp_terms WHERE slug='festivals'
AND NOT EXISTS (
    SELECT 1 FROM wp_term_taxonomy tt
    JOIN wp_terms t ON tt.term_id=t.term_id
    WHERE t.slug='festivals' AND tt.taxonomy='tribe_events_cat'
);

INSERT IGNORE INTO wp_terms (name, slug, term_group)
SELECT 'Family & Community', 'family-community', 0
WHERE NOT EXISTS (SELECT 1 FROM wp_terms WHERE slug='family-community');

INSERT IGNORE INTO wp_term_taxonomy (term_id, taxonomy, description, parent, count)
SELECT term_id, 'tribe_events_cat', '', 0, 0
FROM wp_terms WHERE slug='family-community'
AND NOT EXISTS (
    SELECT 1 FROM wp_term_taxonomy tt
    JOIN wp_terms t ON tt.term_id=t.term_id
    WHERE t.slug='family-community' AND tt.taxonomy='tribe_events_cat'
);

INSERT IGNORE INTO wp_terms (name, slug, term_group)
SELECT 'More To Do', 'more-to-do', 0
WHERE NOT EXISTS (SELECT 1 FROM wp_terms WHERE slug='more-to-do');

INSERT IGNORE INTO wp_term_taxonomy (term_id, taxonomy, description, parent, count)
SELECT term_id, 'tribe_events_cat', '', 0, 0
FROM wp_terms WHERE slug='more-to-do'
AND NOT EXISTS (
    SELECT 1 FROM wp_term_taxonomy tt
    JOIN wp_terms t ON tt.term_id=t.term_id
    WHERE t.slug='more-to-do' AND tt.taxonomy='tribe_events_cat'
);

-- Step 2: Assign categories based on title keywords


-- live-music-concerts
INSERT IGNORE INTO wp_term_relationships (object_id, term_taxonomy_id, term_order)
SELECT p.ID, tt.term_taxonomy_id, 0
FROM wp_posts p
JOIN wp_term_taxonomy tt ON tt.taxonomy='tribe_events_cat'
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='live-music-concerts'
WHERE p.post_type='tribe_events'
AND p.post_status='publish'
AND (LOWER(p.post_title) LIKE '%concert%' OR LOWER(p.post_title) LIKE '%live music%' OR LOWER(p.post_title) LIKE '%live band%' OR LOWER(p.post_title) LIKE '%dj set%' OR LOWER(p.post_title) LIKE '%dj night%' OR LOWER(p.post_title) LIKE '%jazz%' OR LOWER(p.post_title) LIKE '%blues%' OR LOWER(p.post_title) LIKE '%hip hop%' OR LOWER(p.post_title) LIKE '%hip-hop%' OR LOWER(p.post_title) LIKE '%country music%' OR LOWER(p.post_title) LIKE '%rock show%' OR LOWER(p.post_title) LIKE '%indie%' OR LOWER(p.post_title) LIKE '%rap%' OR LOWER(p.post_title) LIKE '%r&b%' OR LOWER(p.post_title) LIKE '%soul%' OR LOWER(p.post_title) LIKE '%folk%' OR LOWER(p.post_title) LIKE '%metal%' OR LOWER(p.post_title) LIKE '%punk%' OR LOWER(p.post_title) LIKE '%singer songwriter%' OR LOWER(p.post_title) LIKE '%open jam%' OR LOWER(p.post_title) LIKE '%open mic%' OR LOWER(p.post_title) LIKE '%touring%' OR LOWER(p.post_title) LIKE '%tour%' OR LOWER(p.post_title) LIKE '%album release%' OR LOWER(p.post_title) LIKE '%the nick%' OR LOWER(p.post_title) LIKE '%hernando%' OR LOWER(p.post_title) LIKE '%overton shell%' OR LOWER(p.post_title) LIKE '%crosstown%' OR LOWER(p.post_title) LIKE '%satellite music%' OR LOWER(p.post_title) LIKE '%minglewood%' OR LOWER(p.post_title) LIKE '%radio rooftop%' OR LOWER(p.post_title) LIKE '%beale street%');

UPDATE wp_term_taxonomy tt
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='live-music-concerts'
SET tt.count=(
    SELECT COUNT(*) FROM wp_term_relationships tr
    WHERE tr.term_taxonomy_id=tt.term_taxonomy_id
)
WHERE tt.taxonomy='tribe_events_cat';


-- comedy
INSERT IGNORE INTO wp_term_relationships (object_id, term_taxonomy_id, term_order)
SELECT p.ID, tt.term_taxonomy_id, 0
FROM wp_posts p
JOIN wp_term_taxonomy tt ON tt.taxonomy='tribe_events_cat'
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='comedy'
WHERE p.post_type='tribe_events'
AND p.post_status='publish'
AND (LOWER(p.post_title) LIKE '%comedy%' OR LOWER(p.post_title) LIKE '%stand-up%' OR LOWER(p.post_title) LIKE '%standup%' OR LOWER(p.post_title) LIKE '%improv%' OR LOWER(p.post_title) LIKE '%comedian%' OR LOWER(p.post_title) LIKE '%laughs%');

UPDATE wp_term_taxonomy tt
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='comedy'
SET tt.count=(
    SELECT COUNT(*) FROM wp_term_relationships tr
    WHERE tr.term_taxonomy_id=tt.term_taxonomy_id
)
WHERE tt.taxonomy='tribe_events_cat';


-- performing-visual-arts
INSERT IGNORE INTO wp_term_relationships (object_id, term_taxonomy_id, term_order)
SELECT p.ID, tt.term_taxonomy_id, 0
FROM wp_posts p
JOIN wp_term_taxonomy tt ON tt.taxonomy='tribe_events_cat'
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='performing-visual-arts'
WHERE p.post_type='tribe_events'
AND p.post_status='publish'
AND (LOWER(p.post_title) LIKE '%theater%' OR LOWER(p.post_title) LIKE '%theatre%' OR LOWER(p.post_title) LIKE '%ballet%' OR LOWER(p.post_title) LIKE '%opera%' OR LOWER(p.post_title) LIKE '%symphony%' OR LOWER(p.post_title) LIKE '%orchestra%' OR LOWER(p.post_title) LIKE '%gallery%' OR LOWER(p.post_title) LIKE '%exhibit%' OR LOWER(p.post_title) LIKE '%museum%' OR LOWER(p.post_title) LIKE '%film series%' OR LOWER(p.post_title) LIKE '%art show%' OR LOWER(p.post_title) LIKE '%art walk%' OR LOWER(p.post_title) LIKE '%drag show%' OR LOWER(p.post_title) LIKE '%burlesque%' OR LOWER(p.post_title) LIKE '%magic show%' OR LOWER(p.post_title) LIKE '%spoken word%' OR LOWER(p.post_title) LIKE '%poetry%' OR LOWER(p.post_title) LIKE '%film series%' OR LOWER(p.post_title) LIKE '%arts film%');

UPDATE wp_term_taxonomy tt
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='performing-visual-arts'
SET tt.count=(
    SELECT COUNT(*) FROM wp_term_relationships tr
    WHERE tr.term_taxonomy_id=tt.term_taxonomy_id
)
WHERE tt.taxonomy='tribe_events_cat';


-- sports-fitness
INSERT IGNORE INTO wp_term_relationships (object_id, term_taxonomy_id, term_order)
SELECT p.ID, tt.term_taxonomy_id, 0
FROM wp_posts p
JOIN wp_term_taxonomy tt ON tt.taxonomy='tribe_events_cat'
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='sports-fitness'
WHERE p.post_type='tribe_events'
AND p.post_status='publish'
AND (LOWER(p.post_title) LIKE '%5k%' OR LOWER(p.post_title) LIKE '%10k%' OR LOWER(p.post_title) LIKE '%marathon%' OR LOWER(p.post_title) LIKE '%half marathon%' OR LOWER(p.post_title) LIKE '%fun run%' OR LOWER(p.post_title) LIKE '%trot%' OR LOWER(p.post_title) LIKE '%yoga%' OR LOWER(p.post_title) LIKE '%pilates%' OR LOWER(p.post_title) LIKE '%zumba%' OR LOWER(p.post_title) LIKE '%barre%' OR LOWER(p.post_title) LIKE '%spin class%' OR LOWER(p.post_title) LIKE '%cycling class%' OR LOWER(p.post_title) LIKE '%boot camp%' OR LOWER(p.post_title) LIKE '%bootcamp%' OR LOWER(p.post_title) LIKE '%crossfit%' OR LOWER(p.post_title) LIKE '%fitness%' OR LOWER(p.post_title) LIKE '%wellness series%' OR LOWER(p.post_title) LIKE '%grizzlies%' OR LOWER(p.post_title) LIKE '%hustle%' OR LOWER(p.post_title) LIKE '%tigers%' OR LOWER(p.post_title) LIKE '%redbirds%' OR LOWER(p.post_title) LIKE '%barons%' OR LOWER(p.post_title) LIKE '%nba%' OR LOWER(p.post_title) LIKE '%nfl%' OR LOWER(p.post_title) LIKE '%mlb%' OR LOWER(p.post_title) LIKE '%nhl%' OR LOWER(p.post_title) LIKE '%mls%' OR LOWER(p.post_title) LIKE '%soccer%' OR LOWER(p.post_title) LIKE '%football game%' OR LOWER(p.post_title) LIKE '%basketball%' OR LOWER(p.post_title) LIKE '%baseball%' OR LOWER(p.post_title) LIKE '%hockey%' OR LOWER(p.post_title) LIKE '%tennis%' OR LOWER(p.post_title) LIKE '%golf tournament%' OR LOWER(p.post_title) LIKE '%pickleball%' OR LOWER(p.post_title) LIKE '%triathlon%' OR LOWER(p.post_title) LIKE '%swim%');

UPDATE wp_term_taxonomy tt
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='sports-fitness'
SET tt.count=(
    SELECT COUNT(*) FROM wp_term_relationships tr
    WHERE tr.term_taxonomy_id=tt.term_taxonomy_id
)
WHERE tt.taxonomy='tribe_events_cat';


-- festivals
INSERT IGNORE INTO wp_term_relationships (object_id, term_taxonomy_id, term_order)
SELECT p.ID, tt.term_taxonomy_id, 0
FROM wp_posts p
JOIN wp_term_taxonomy tt ON tt.taxonomy='tribe_events_cat'
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='festivals'
WHERE p.post_type='tribe_events'
AND p.post_status='publish'
AND (LOWER(p.post_title) LIKE '%festival%' OR LOWER(p.post_title) LIKE '%street fair%' OR LOWER(p.post_title) LIKE '%street fest%' OR LOWER(p.post_title) LIKE '%block party%' OR LOWER(p.post_title) LIKE '%carnival%' OR LOWER(p.post_title) LIKE '%flea market%' OR LOWER(p.post_title) LIKE '%holiday market%' OR LOWER(p.post_title) LIKE '%pride festival%' OR LOWER(p.post_title) LIKE '%oktoberfest%');

UPDATE wp_term_taxonomy tt
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='festivals'
SET tt.count=(
    SELECT COUNT(*) FROM wp_term_relationships tr
    WHERE tr.term_taxonomy_id=tt.term_taxonomy_id
)
WHERE tt.taxonomy='tribe_events_cat';


-- family-community
INSERT IGNORE INTO wp_term_relationships (object_id, term_taxonomy_id, term_order)
SELECT p.ID, tt.term_taxonomy_id, 0
FROM wp_posts p
JOIN wp_term_taxonomy tt ON tt.taxonomy='tribe_events_cat'
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='family-community'
WHERE p.post_type='tribe_events'
AND p.post_status='publish'
AND (LOWER(p.post_title) LIKE '%family friendly%' OR LOWER(p.post_title) LIKE '%kids%' OR LOWER(p.post_title) LIKE '%children%' OR LOWER(p.post_title) LIKE '%youth%' OR LOWER(p.post_title) LIKE '%storytime%' OR LOWER(p.post_title) LIKE '%all ages%' OR LOWER(p.post_title) LIKE '%toddler%' OR LOWER(p.post_title) LIKE '%community event%' OR LOWER(p.post_title) LIKE '%volunteer%' OR LOWER(p.post_title) LIKE '%charity%' OR LOWER(p.post_title) LIKE '%fundraiser%');

UPDATE wp_term_taxonomy tt
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='family-community'
SET tt.count=(
    SELECT COUNT(*) FROM wp_term_relationships tr
    WHERE tr.term_taxonomy_id=tt.term_taxonomy_id
)
WHERE tt.taxonomy='tribe_events_cat';


-- more-to-do: assign to any event with no tribe_events_cat yet
INSERT IGNORE INTO wp_term_relationships (object_id, term_taxonomy_id, term_order)
SELECT p.ID, tt.term_taxonomy_id, 0
FROM wp_posts p
JOIN wp_term_taxonomy tt ON tt.taxonomy='tribe_events_cat'
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='more-to-do'
WHERE p.post_type='tribe_events'
AND p.post_status='publish'
AND p.ID NOT IN (
    SELECT DISTINCT tr.object_id
    FROM wp_term_relationships tr
    JOIN wp_term_taxonomy tt2 ON tr.term_taxonomy_id=tt2.term_taxonomy_id
    WHERE tt2.taxonomy='tribe_events_cat'
);

UPDATE wp_term_taxonomy tt
JOIN wp_terms t ON tt.term_id=t.term_id AND t.slug='more-to-do'
SET tt.count=(
    SELECT COUNT(*) FROM wp_term_relationships tr
    WHERE tr.term_taxonomy_id=tt.term_taxonomy_id
)
WHERE tt.taxonomy='tribe_events_cat';
