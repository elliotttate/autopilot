#pragma once

#include "CoreMinimal.h"

namespace PlaytestBlueprintErrors
{
	/** Why Play In Editor shouldn't start now, "" when it can: the blueprints with compile errors, each with the
	 *  compiler's errors, in words for the person (a run's result). As the engine does before it plays (and then asks in
	 *  a dialog whether to play anyway), the loaded blueprints changed since they last compiled are compiled first when
	 *  the Play settings compile them on Play (Auto Recompile Blueprints). A blueprint the person chose to play with
	 *  anyway (the engine's dialog) doesn't count until it compiles again, as for the engine. A game with broken
	 *  blueprints plays something else than they say, so a run on it would report on the wrong game.
	 *  bCompile false (a restart while the game plays: nothing is compiled under it): only the blueprints already in
	 *  error, without the compiler's words. */
	FString Describe(bool bCompile);
}
