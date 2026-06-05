# Void Patrol — Game Design Document

## Overview

A vertical-scrolling top-down space shooter. The player pilots a fighter ship, shooting down waves of enemy ships and avoiding bullets and collisions. Survive all waves and defeat the boss to win. The game loops with increased difficulty.

## Core Mechanics

### Player Ship
- Moves in all 4 directions, keyboard (WASD or arrows) or mouse position
- Clamped to screen bounds
- Fires projectiles upward automatically at a fixed rate (can be upgraded)
- Has a shield bar (health) — depletes on enemy bullet or collision contact
- Starts with 3 lives; losing all shield on one life costs a life and respawns at bottom center

### Shooting
- Default: single shot, fires every 0.3s
- Power-up upgrades: double shot, triple spread, laser beam (see power-ups)
- Player bullets destroy enemies on contact (1 hit for basic enemies)
- Shot type resets to single on life loss

### Enemies
Three standard enemy types plus a boss:

| Type | HP | Speed | Fire pattern | Points |
|------|----|-------|--------------|--------|
| Drone | 1 | medium, straight down | none | 10 |
| Fighter | 2 | medium, slight weave | single shot downward | 25 |
| Bomber | 4 | slow | burst of 3 angled shots | 50 |
| Boss | 40 | slow horizontal sweep | multiple patterns (see below) | 500 |

### Enemy Waves
- 6 waves before the boss
- Each wave spawns from a predefined formation pattern (V, line, flanking, etc.)
- Wave clears when all enemies are destroyed; next wave spawns after 2s delay
- Enemies not destroyed before the next wave spawn together (overlap is intentional tension)
- Boss spawns after wave 6

### Boss
- Enters from top, sweeps horizontally
- 3 attack phases based on HP thresholds (100%, 60%, 30%):
  - **Phase 1**: fires aimed single shots at player every 1.5s
  - **Phase 2**: adds a spread burst every 3s
  - **Phase 3**: adds a rotating ring of bullets every 5s
- Weak point: glowing core in center (takes 2× damage)
- Defeated boss → victory screen → loop with +1 difficulty

### Power-ups
Drop from destroyed Fighters and Bombers (~25% chance). Auto-collected on contact.

| Power-up | Color | Effect | Duration |
|----------|-------|--------|----------|
| Double Shot | yellow | fires 2 parallel bullets | 15s |
| Triple Spread | orange | fires 3 bullets in a spread | 12s |
| Laser | red | continuous laser beam, infinite pierce | 8s |
| Shield Boost | blue | restores 50% of shield bar | instant |
| Speed Boost | green | player move speed ×1.4 | 10s |
| Bomb | purple | destroys all bullets on screen, damages all enemies for 2 HP | instant |

Only one shot-type power-up active at a time (new one replaces old).

### Scoring
- Enemy kills: per type (see table above)
- Wave clear bonus: 100 × wave number
- Boss kill bonus: 500
- No-hit wave bonus: 200 (if player took 0 damage during a wave)
- High score persisted across sessions

### Lives & Shield
- 3 lives, max 5 (Extra Life power-up could be added in future)
- Shield: 100 HP. Drone collision: −20. Fighter bullet: −15. Bomber burst: −25. Boss attacks: −20 to −35.
- Shield does not regenerate (only Shield Boost power-up restores it)
- Shield bar displayed in HUD; flashes red when below 25%

## Scenes & Structure

```
res://
  scenes/
    main.tscn           # root: game state, wave manager, HUD
    game.tscn           # gameplay: player, enemies, bullets, power-ups
    player.tscn
    enemy_drone.tscn
    enemy_fighter.tscn
    enemy_bomber.tscn
    boss.tscn
    bullet_player.tscn
    bullet_enemy.tscn
    powerup.tscn
    explosion.tscn
    hud.tscn
    game_over.tscn
    victory.tscn
    menu.tscn
  scripts/
    main.gd
    game.gd
    player.gd
    enemy_base.gd       # shared enemy logic
    enemy_drone.gd
    enemy_fighter.gd
    enemy_bomber.gd
    boss.gd
    bullet.gd
    powerup.gd
    wave_manager.gd     # spawns waves, tracks clears
    hud.gd
  data/
    waves.json          # wave formation definitions
  autoload/
    state_server.gd
    game_state.gd       # score, lives, wave, high_score
```

## Game States

- `menu` → `playing` → `wave_clear` → `playing` (next wave)
- `playing` → `boss_fight` (after wave 6)
- `boss_fight` → `victory` → `menu` (loop with difficulty +1)
- `playing` / `boss_fight` → `game_over` (lives == 0)
- `game_over` → `menu`

## HUD

- Top: Score (left), Wave N/6 or "BOSS" (center), High Score (right)
- Bottom-left: Lives as ship icons
- Bottom-right: Shield bar
- Active power-up name + remaining duration shown above shield bar
- "WAVE CLEAR" / "INCOMING" banners displayed between waves

## Scrolling Background
- Slow parallax starfield (2 layers, different speeds)
- Purely visual, no gameplay effect

## Audio
- Player shot, enemy shot sounds
- Explosion (small for drones, larger for bombers/boss)
- Power-up pickup chime
- Shield hit sound + screen flash
- Life lost jingle
- Wave clear fanfare
- Boss music (distinct track or intensity shift)
- Victory / game-over stings

## Visual Style
- Dark space background with parallax stars
- Player ship: sleek blue/white fighter
- Enemies: distinct silhouettes per type (round drone, angular fighter, bulky bomber)
- Boss: large, imposing, with visible weak point glow
- Bullets: player = bright cyan, enemy = red/orange
- Explosions: particle burst
- Power-ups: glowing colored orbs with icon

## Win / Fail Conditions (for QA)

- **Win**: boss HP reaches 0 → victory screen shown
- **Fail**: lives reach 0 → game_over screen shown
- Player bullets must destroy enemies on contact
- Enemy bullets must reduce shield on contact
- Power-ups must activate on collection and expire correctly
- Wave counter must increment correctly and boss must spawn after wave 6
- Score must increment on each kill

## get_game_state() shape

```gdscript
func get_game_state() -> Dictionary:
    return {
        "scene": "playing",          # menu / playing / boss_fight / victory / game_over
        "score": score,
        "high_score": high_score,
        "lives": lives,
        "shield": shield,
        "wave": current_wave,        # 1-6, or 7 = boss
        "enemies_remaining": enemies_remaining,
        "active_powerup": active_powerup,   # string or ""
        "powerup_time_left": powerup_time_left,
        "boss_hp": boss_hp           # -1 if no boss active
    }
```
