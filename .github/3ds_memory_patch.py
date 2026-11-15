#!/usr/bin/env python3
"""3DS memory + controls patches for re-plants-vs-zombies.

Applied only in the 3DS CI build. Every edit must match exactly the expected
number of times, otherwise the script stops with an error so the build never
silently goes ahead with a half-applied patch.
"""
import re
import sys

INPUT_CPP = r"""#include <3ds.h>
#include <string>

#include "SexyAppBase.h"
#include "graphics/GLInterface.h"
#include "graphics/GLImage.h"
#include "widget/WidgetManager.h"

using namespace Sexy;

// 3DS controls (the game is drawn on the bottom touch screen):
//   Touch screen  = left mouse button (pick seeds, plant, collect sun, menus)
//   B or R        = right mouse button (drop the seed / shovel you're holding)
//   A             = Enter (confirm dialogs)
//   START         = Escape (game menu)
//   SELECT        = Space (pause)
//   HOME / POWER  = handled by the system, the game quits cleanly

static int gLastMouseX = 0;
static int gLastMouseY = 0;

struct ButtonKey { u32 mButton; KeyCode mKey; };
static const ButtonKey gButtonKeys[] = {
	{ KEY_A,      KEYCODE_RETURN },
	{ KEY_START,  KEYCODE_ESCAPE },
	{ KEY_SELECT, KEYCODE_SPACE  },
};

void SexyAppBase::InitInput()
{
	if (!mMouseIn)
		mMouseIn = true;
}

bool SexyAppBase::StartTextInput(std::string& theInput)
{
	char aBuf[64] = { 0 };

	SwkbdState aKbd;
	swkbdInit(&aKbd, SWKBD_TYPE_NORMAL, 2, 32);
	swkbdSetHintText(&aKbd, "Enter your name");
	if (!theInput.empty())
		swkbdSetInitialText(&aKbd, theInput.c_str());

	SwkbdButton aButton = swkbdInputText(&aKbd, aBuf, sizeof(aBuf));
	if (aButton == SWKBD_BUTTON_CONFIRM)
	{
		theInput = aBuf;
		return true;
	}
	return false;
}

void SexyAppBase::StopTextInput()
{
}

bool SexyAppBase::ProcessDeferredMessages(bool singleMessage)
{
	(void)singleMessage;

	// Lets HOME, sleep and power off work; quit when the system asks us to
	if (!aptMainLoop())
	{
		mShutdown = true;
		return false;
	}

	if (mWidgetManager == NULL)
		return false;

	hidScanInput();
	u32 aDown = hidKeysDown();
	u32 aUp = hidKeysUp();
	u32 aHeld = hidKeysHeld();

	if (aDown | aUp)
		mLastUserInputTick = mLastTimerTime;

	// Touch screen -> left mouse button
	if (aHeld & KEY_TOUCH)
	{
		touchPosition aTouch;
		hidTouchRead(&aTouch);
		int x = aTouch.px;
		int y = aTouch.py;
		mWidgetManager->RemapMouse(x, y);
		gLastMouseX = x;
		gLastMouseY = y;

		mLastUserInputTick = mLastTimerTime;
		mWidgetManager->MouseMove(x, y);
		if (aDown & KEY_TOUCH)
			mWidgetManager->MouseDown(x, y, 1);
	}
	else if (aUp & KEY_TOUCH)
	{
		// The touch position reads as 0,0 after release, so use the last one
		mWidgetManager->MouseUp(gLastMouseX, gLastMouseY, 1);
	}

	// B / R -> right mouse button at the last touched spot
	if (aDown & (KEY_B | KEY_R))
		mWidgetManager->MouseDown(gLastMouseX, gLastMouseY, -1);
	if (aUp & (KEY_B | KEY_R))
		mWidgetManager->MouseUp(gLastMouseX, gLastMouseY, -1);

	// Buttons -> keyboard keys
	for (const ButtonKey& aKey : gButtonKeys)
	{
		if (aDown & aKey.mButton)
			mWidgetManager->KeyDown(aKey.mKey);
		if (aUp & aKey.mButton)
			mWidgetManager->KeyUp(aKey.mKey);
	}

	return false;
}
"""

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
TEX16 = (sys.argv[2] if len(sys.argv) > 2 else "1") == "1"


def edit(path, pattern, repl, count=1, regex=False):
    full = f"{ROOT}/{path}"
    with open(full, encoding="utf-8", errors="surrogateescape") as f:
        text = f.read()
    if regex:
        found = len(re.findall(pattern, text, flags=re.S))
    else:
        found = text.count(pattern)
    if found != count:
        sys.exit(f"PATCH FAILED: {path}: expected {count} match(es), found {found}\n  pattern: {pattern!r}")
    if regex:
        text = re.sub(pattern, lambda m: repl, text, flags=re.S)
    else:
        text = text.replace(pattern, repl)
    with open(full, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)
    print(f"patched {path}")


# ---------------------------------------------------------------------------
# 1. Stream main.pak from the SD card instead of loading all ~45 MB into RAM
# ---------------------------------------------------------------------------
PAK_H = "SexyAppFramework/paklib/PakInterface.h"
PAK_CPP = "SexyAppFramework/paklib/PakInterface.cpp"

edit(PAK_H, "#include <cstdint>\n",
     "#include <cstdint>\n#include <cstdio>\n#include <cstdlib>\n\n#define PAK_CACHE_SIZE (128 * 1024)\n")

edit(PAK_H,
     r"explicit PakCollection\(size_t size\)\s*\{\s*mDataPtr = malloc\(size\);\s*\}\s*~PakCollection\(\)\s*\{\s*free\(mDataPtr\);\s*\}",
     """FILE*						mFP = nullptr;			// 3DS: pak stays on the SD card
	size_t						mFileSize = 0;
	unsigned char*				mCache = nullptr;		// small decoded read cache
	size_t						mCacheStart = 0;
	size_t						mCacheLen = 0;

	explicit PakCollection(size_t size) : mFileSize(size) { mDataPtr = nullptr; mCache = (unsigned char*)malloc(PAK_CACHE_SIZE); }
	~PakCollection() { if (mFP) fclose(mFP); free(mCache); }

	size_t ReadAt(size_t thePos, void* theDest, size_t theCount);
	int GetByte(size_t thePos) { unsigned char c = 0; return ReadAt(thePos, &c, 1) == 1 ? (int)c : 0; }""",
     regex=True)

edit(PAK_CPP, '#include "fcaseopen/fcaseopen.h"\n',
     '''#include "fcaseopen/fcaseopen.h"
#include <3ds.h>
#include <algorithm>
#include <cstring>

// One lock for all pak reads: resources are loaded from a background thread too.
static LightLock gPakLock;
static struct PakLockInitializer { PakLockInitializer() { LightLock_Init(&gPakLock); } } gPakLockInitializer;

size_t PakCollection::ReadAt(size_t thePos, void* theDest, size_t theCount)
{
	if (mFP == nullptr || thePos >= mFileSize || theCount == 0)
		return 0;
	if (theCount > mFileSize - thePos)
		theCount = mFileSize - thePos;

	unsigned char* aDest = (unsigned char*)theDest;
	size_t aDone = 0;

	LightLock_Lock(&gPakLock);
	while (aDone < theCount)
	{
		size_t aPos = thePos + aDone;
		size_t aLeft = theCount - aDone;

		// Served from cache?
		if (mCache != nullptr && mCacheLen > 0 && aPos >= mCacheStart && aPos < mCacheStart + mCacheLen)
		{
			size_t n = std::min(aLeft, mCacheStart + mCacheLen - aPos);
			memcpy(aDest + aDone, mCache + (aPos - mCacheStart), n);
			aDone += n;
			continue;
		}

		if (fseek(mFP, (long)aPos, SEEK_SET) != 0)
			break;

		// Big reads (images, sounds) go straight into the caller's buffer
		if (mCache == nullptr || aLeft >= PAK_CACHE_SIZE)
		{
			size_t n = fread(aDest + aDone, 1, aLeft, mFP);
			for (size_t i = 0; i < n; i++)
				aDest[aDone + i] ^= 0xF7;
			aDone += n;
			break;
		}

		// Small reads (text, headers) refill the cache
		size_t n = fread(mCache, 1, PAK_CACHE_SIZE, mFP);
		if (n == 0)
			break;
		for (size_t i = 0; i < n; i++)
			mCache[i] ^= 0xF7;
		mCacheStart = aPos;
		mCacheLen = n;
	}
	LightLock_Unlock(&gPakLock);
	return aDone;
}
''')

# Keep the file open instead of reading + decoding the whole pak into memory
edit(PAK_CPP,
     r"if \(fread\(aPakCollection->mDataPtr, 1, aFileSize, aFileHandle\) != aFileSize\) \{.*?\n(?=\s*PakRecordMap::iterator aRecordItr = mPakRecordMap\.insert)",
     "setvbuf(aFileHandle, NULL, _IONBF, 0);\n\taPakCollection->mFP = aFileHandle;\n",
     regex=True)

# FRead: read from the SD card instead of a memcpy out of the in-memory pak
edit(PAK_CPP,
     r"uchar\* src = \(uchar\*\) theFile->mRecord->mCollection->mDataPtr[^;]*;\s*uchar\* dest = \(uchar\*\) thePtr;\s*memcpy\(dest, src, aSizeBytes\);",
     "aSizeBytes = (aSizeBytes > 0) ? (int)theFile->mRecord->mCollection->ReadAt(theFile->mRecord->mStartPos + theFile->mPos, thePtr, aSizeBytes) : 0;",
     regex=True)

# FGetC / FGetS: byte reads go through the cache
edit(PAK_CPP,
     r"\*\(\(char\*\) theFile->mRecord->mCollection->mDataPtr \+ theFile->mRecord->mStartPos \+ theFile->mPos\+\+\)",
     "(char)theFile->mRecord->mCollection->GetByte(theFile->mRecord->mStartPos + theFile->mPos++)",
     count=2, regex=True)

# ---------------------------------------------------------------------------
# 2. Mix audio at 22 kHz mono: decoded sound effects use ~4x less memory
#    (the game never pans sounds, so mono loses nothing)
# ---------------------------------------------------------------------------
edit("SexyAppFramework/sound/SDLSoundManager.cpp",
     "Mix_OpenAudio(44100, AUDIO_S16SYS, 2, 2048)",
     "Mix_OpenAudio(22050, AUDIO_S16SYS, 1, 1024)")

# ---------------------------------------------------------------------------
# 3. 16-bit textures for images with transparency (half the texture memory)
# ---------------------------------------------------------------------------
GL = "SexyAppFramework/platform/3ds/graphics/GLInterface.cpp"
if TEX16:
    edit(GL,
         "gSupportedPixelFormats = PixelFormat_A8R8G8B8 | PixelFormat_A4R4G4B4 | PixelFormat_R5G6B5 | PixelFormat_Palette8;",
         "gSupportedPixelFormats = PixelFormat_A4R4G4B4 | PixelFormat_R5G6B5 | PixelFormat_Palette8;")

# Vertex buffer comes from linearAlloc, so it must be freed with linearFree
edit(GL, "delete[] gVertices;", "linearFree(gVertices);")

# ---------------------------------------------------------------------------
# 4. Controls: the 3DS input code was still commented-out Switch code,
#    so the game had no controls at all. Replace it with a real 3DS version.
# ---------------------------------------------------------------------------
INPUT = "SexyAppFramework/platform/3ds/Input.cpp"
with open(f"{ROOT}/{INPUT}", encoding="utf-8") as f:
    old = f.read()
if "padUpdate(&pad)" not in old or "/*" not in old:
    sys.exit(f"PATCH FAILED: {INPUT} no longer looks like the old placeholder; check it by hand")
with open(f"{ROOT}/{INPUT}", "w", encoding="utf-8") as f:
    f.write(INPUT_CPP)
print(f"replaced {INPUT}")

print("All 3DS patches applied.")
