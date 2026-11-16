#include "PlaytestBlueprintErrors.h"

#include "Engine/Blueprint.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Settings/LevelEditorPlaySettings.h"
#include "UObject/Package.h"
#include "UObject/UObjectIterator.h"

FString PlaytestBlueprintErrors::Describe(bool bCompile)
{
	const ULevelEditorPlaySettings* Settings = GetDefault<ULevelEditorPlaySettings>();
	const bool bCompileDirty = bCompile && (!Settings || Settings->AutoRecompileBlueprints);
	TArray<UBlueprint*> Checked;
	for (TObjectIterator<UBlueprint> It; It; ++It)
	{
		UBlueprint* Blueprint = *It;
		if (!IsValid(Blueprint) || Blueprint->HasAnyFlags(RF_ClassDefaultObject | RF_Transient)
			|| Blueprint->GetPackage()->HasAnyPackageFlags(PKG_PlayInEditor))
		{
			continue;
		}
		const bool bDirty = bCompileDirty && Blueprint->Status != BS_Unknown && Blueprint->IsPossiblyDirty()
			&& !FBlueprintEditorUtils::IsDataOnlyBlueprint(Blueprint);
		if (bDirty || (Blueprint->Status == BS_Error && Blueprint->bDisplayCompilePIEWarning))
		{
			Checked.Add(Blueprint);
		}
	}
	FString Listed;
	int32 Broken = 0;
	for (UBlueprint* Blueprint : Checked)
	{
		// (compiled, also when already in error: a blueprint keeps no words of its errors, its compile has them)
		FCompilerResultsLog Results;
		Results.bSilentMode = true;
		if (bCompile)
		{
			FKismetEditorUtilities::CompileBlueprint(Blueprint, EBlueprintCompileOptions::SkipGarbageCollection, &Results);
		}
		if (Blueprint->Status != BS_Error)
		{
			continue;
		}
		++Broken;
		Listed += FString::Printf(TEXT("\n- %s (%s)"), *Blueprint->GetName(), *Blueprint->GetPathName());
		for (const TSharedRef<FTokenizedMessage>& Message : Results.Messages)
		{
			if (Message->GetSeverity() == EMessageSeverity::Error)
			{
				Listed += TEXT("\n    ") + Message->ToText().ToString();
			}
		}
	}
	if (Broken == 0)
	{
		return FString();
	}
	const bool bOne = Broken == 1;
	return FString::Printf(TEXT("The game wasn't started: %s compile errors, so the game wouldn't play as %s.%s\n"
		"Fix %s in the editor (open %s and press Compile to see where) and run again. Or press Play once and choose to "
		"play anyway: runs then play with %s too, until %s again."),
		bOne ? TEXT("this blueprint has") : TEXT("these blueprints have"), bOne ? TEXT("it says") : TEXT("they say"),
		*Listed, bOne ? TEXT("it") : TEXT("them"), bOne ? TEXT("it") : TEXT("each"), bOne ? TEXT("it") : TEXT("them"),
		bOne ? TEXT("it compiles") : TEXT("they compile"));
}
