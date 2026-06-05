# Brick Breaker — Game Design Document

## Overview

A single-player Arkanoid-style brick breaker. The player controls a paddle at the bottom of the screen to keep a ball in play and destroy all bricks. Clear all bricks to advance to the next level. Lose all lives to get a game over.

## Core Mechanics

### Paddle
- Moves horizontally only, controlled by mouse or left/right arrow keys
- Clamped to screen edges
- Width: 120px. Slightly narrows every 3 levels (min 80px)

### Ball
- Starts stationary on the paddle; launches on Space or click
- Constant speed; bounces off walls (top, left, right), paddle, and bricks
- Angle off paddle depends on hit position: center = straight up, edges = sharp angle
- If ball exits bottom: lose a life, ball resets on paddle
- Speed increases slightly each level (cap at 1.5× starting speed)

### Bricks
- Grid layout: 10 columns × 6 rows, centered, with small gaps
- Brick types:
  - **Normal** (1 hit, white/grey) — worth 10 points
  - **Tough** (2 hits, blue → cracks on first hit) — worth 25 points
  - **Hard** (3 hits, red → orange → cracks) — worth 50 points
  - **Indestructible** (silver, never breaks) — worth 0, acts as wall
- Level 1: all Normal. Later levels introduce Tough, Hard, and Indestructible bricks

### Power-ups
Drop from destroyed bricks with ~20% probability. Fall slowly; caught by paddle contact.

| Power-up | Color | Effect | Duration |
|----------|-------|--------|----------|
| Wide Paddle | green | paddle width ×1.5 | 15s |
| Multi-ball | yellow | splits ball into 3 | until 1 ball remains |
| Slow Ball | blue | ball speed ×0.7 | 10s |
| Laser | red | paddle fires lasers upward on Space | 10s |
| Extra Life | pink | +1 life (instant) | — |

### Scoring
- Normal brick: 10 pts
- Tough brick: 25 pts
- Hard brick: 50 pts
- Combo multiplier: consecutive brick hits without missing increment a ×1→×2→×3→×4 multiplier (resets on miss or ball loss)
- Level clear bonus: 500 × level number

### Lives
- Start with 3 lives
- Game over at 0 lives
- Max lives: 5

### Levels
- 5 levels total, looping back to level 1 with increased difficulty after level 5
- Each level has a hand-authored brick layout defined in a resource or data file
- Difficulty scaling per loop: ball speed cap +5%, more Tough/Hard bricks

## Scenes & Structure

```
res://
  scenes/
    main.tscn          # root: manages game state, level loading, HUD
    game.tscn          # gameplay area: paddle, ball, brick grid, power-ups
    paddle.tscn
    ball.tscn
    brick.tscn
    powerup.tscn
    hud.tscn           # score, lives, level display
    game_over.tscn
    level_complete.tscn
  scripts/
    main.gd
    game.gd
    paddle.gd
    ball.gd
    brick.gd
    powerup.gd
    hud.gd
  data/
    levels/
      level_1.tres     # brick layout resource
      level_2.tres
      level_3.tres
      level_4.tres
      level_5.tres
  autoload/
    state_server.gd
    game_state.gd      # singleton: score, lives, current_level, high_score
```

## Game States

- `menu` → `playing` → `level_complete` → `playing` (next level)
- `playing` → `game_over` (lives == 0)
- `game_over` → `menu`

## HUD

- Top bar: Score (left), Level (center), Lives as heart icons (right)
- High score displayed below score
- Combo multiplier shown briefly when > ×1

## Audio

- Ball bounce (wall, paddle, brick) — distinct sounds
- Brick destroy sound (varies by type)
- Power-up pickup chime
- Life lost jingle
- Level clear fanfare
- Game over sting

## Visual Style

- Dark background (space or dark blue)
- Bricks use simple colored rectangles with a subtle gradient or border
- Ball is a glowing white circle
- Paddle is a rounded rectangle
- Power-ups are small labeled capsules that fall with a gentle glow

## Win / Fail Conditions (for QA)

- **Win**: all destructible bricks cleared → level_complete screen shown → next level loads
- **Fail**: lives reach 0 → game_over screen shown
- Ball must always be moving when in play
- Score must increment correctly on brick destruction
- Power-ups must activate on paddle contact and expire after duration

## get_game_state() shape

```gdscript
func get_game_state() -> Dictionary:
    return {
        "scene": "playing",          # menu / playing / level_complete / game_over
        "score": score,
        "high_score": high_score,
        "lives": lives,
        "level": current_level,
        "bricks_remaining": bricks_remaining,
        "ball_in_play": ball_in_play,
        "active_powerups": active_powerups,  # array of strings
        "combo_multiplier": combo_multiplier
    }
```
