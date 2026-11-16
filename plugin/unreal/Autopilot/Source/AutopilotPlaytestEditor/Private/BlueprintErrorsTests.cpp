// A run never plays a game whose blueprints don't compile (PlaytestBlueprintErrors): a session start answers 412 with
// each broken blueprint and the compiler's errors, before anything starts, instead of the engine's dialog asking
// whether to play anyway; a blueprint changed since it compiled is compiled first, as Play does; one the person chose
// to play with anyway (the engine's dialog) doesn't stop a run; a fixed one doesn't either. Plays nothing: the
// session's handler is a stand-in.

#include "Components/PrimitiveComponent.h"
#include "EdGraph/EdGraph.h"
#include "EdGraphSchema_K2.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "GameFramework/Actor.h"
#include "K2Node_CallFunction.h"
#include "K2Node_CustomEvent.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Misc/AutomationTest.h"
#include "PlaytestBlueprintErrors.h"
#include "PlaytestJson.h"
#include "PlaytestSubsystem.h"
#include "UObject/Package.h"
#include "UObject/StrongObjectPtr.h"

#if WITH_DEV_AUTOMATION_TESTS

namespace
{
	/** An actor blueprint whose custom event calls Set Simulate Physics with no Target: a primitive component's
	 *  function in an actor, so "Target must have a connection" (the call node, for taking it out again). */
	UBlueprint* BrokenBlueprint(UK2Node_CallFunction*& OutCall)
	{
		UPackage* Package = CreatePackage(TEXT("/Temp/Autopilot/BP_AutopilotBroken"));
		UBlueprint* Blueprint = FKismetEditorUtilities::CreateBlueprint(AActor::StaticClass(), Package,
			TEXT("BP_AutopilotBroken"), BPTYPE_Normal, UBlueprint::StaticClass(), UBlueprintGeneratedClass::StaticClass());
		UEdGraph* Graph = FBlueprintEditorUtils::FindEventGraph(Blueprint);
		FGraphNodeCreator<UK2Node_CustomEvent> EventCreator(*Graph);
		UK2Node_CustomEvent* Event = EventCreator.CreateNode();
		Event->CustomFunctionName = TEXT("AutopilotBroken");
		EventCreator.Finalize();
		FGraphNodeCreator<UK2Node_CallFunction> CallCreator(*Graph);
		UK2Node_CallFunction* Call = CallCreator.CreateNode();
		Call->SetFromFunction(UPrimitiveComponent::StaticClass()->FindFunctionByName(
			GET_FUNCTION_NAME_CHECKED(UPrimitiveComponent, SetSimulatePhysics)));
		CallCreator.Finalize();
		Event->FindPinChecked(UEdGraphSchema_K2::PN_Then)->MakeLinkTo(Call->GetExecPin());
		OutCall = Call;
		return Blueprint;
	}
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAutopilotBlueprintErrorsTest, "AutopilotPlaytest.Editor.BlueprintErrors",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FAutopilotBlueprintErrorsTest::RunTest(const FString& Parameters)
{
	UK2Node_CallFunction* Call = nullptr;
	UBlueprint* Blueprint = BrokenBlueprint(Call);
	FCompilerResultsLog Silent;  // (its errors kept out of the log, as Describe's are: they'd fail the test)
	Silent.bSilentMode = true;
	FKismetEditorUtilities::CompileBlueprint(Blueprint, EBlueprintCompileOptions::SkipGarbageCollection, &Silent);
	// The session op's answer to a start, through a subsystem of its own (what it sends kept); bHandled: whether the
	// session's handler (Play In Editor) was asked
	auto Start = [](bool& bHandled) -> TSharedPtr<FJsonObject>
	{
		TStrongObjectPtr<UPlaytestSubsystem> Bridge(NewObject<UPlaytestSubsystem>());
		TArray<FString> Sent;
		Bridge->TestSent = [&Sent](const FString& Text) { Sent.Add(Text); };
		bHandled = false;
		Bridge->SessionHandler = [&bHandled](const FString&, const FString&, TFunction<void(bool, const FString&)> Done)
		{
			bHandled = true;
			Done(false, FString());
		};
		Bridge->StartProblem = []() { return PlaytestBlueprintErrors::Describe(true); };
		Bridge->HandleMessage(TEXT("{\"type\": \"request\", \"id\": 7, \"op\": \"session\", \"args\": {\"action\": \"start\"}}"));
		for (const FString& Text : Sent)
		{
			const TSharedPtr<FJsonObject> Message = PlaytestJson::Parse(Text);
			if (Message.IsValid() && Message->GetStringField(TEXT("type")) == TEXT("response"))
			{
				return Message;
			}
		}
		return nullptr;
	};
	TestEqual(TEXT("the blueprint doesn't compile"), (int32)Blueprint->Status, (int32)BS_Error);

	// A start: refused before Play In Editor is asked, with the blueprint and the compiler's words
	bool bHandled = false;
	TSharedPtr<FJsonObject> Answer = Start(bHandled);
	TestTrue(TEXT("answered"), Answer.IsValid());
	if (!Answer.IsValid())
	{
		return false;
	}
	TestFalse(TEXT("not ok"), Answer->GetBoolField(TEXT("ok")));
	TestEqual(TEXT("412"), (int32)Answer->GetNumberField(TEXT("status")), 412);
	const FString Error = Answer->GetStringField(TEXT("error"));
	AddInfo(TEXT("The answer's words: ") + Error);
	TestTrue(TEXT("says the game wasn't started"), Error.StartsWith(TEXT("The game wasn't started: this blueprint has compile errors")));
	TestTrue(TEXT("names the blueprint"), Error.Contains(TEXT("- BP_AutopilotBroken (/Temp/Autopilot/BP_AutopilotBroken.BP_AutopilotBroken)")));
	TestTrue(TEXT("with the compiler's words"), Error.Contains(TEXT("must have a connection")));
	TestFalse(TEXT("Play In Editor wasn't asked"), bHandled);
	// A restart while the game plays: the blueprint in error, nothing compiled
	TestTrue(TEXT("listed without compiling"), PlaytestBlueprintErrors::Describe(false).Contains(TEXT("BP_AutopilotBroken")));

	// The person chose to play anyway (the engine's dialog): runs play too, until it compiles again
	Blueprint->bDisplayCompilePIEWarning = false;
	TestEqual(TEXT("played anyway: nothing stops the run"), PlaytestBlueprintErrors::Describe(true), FString());
	Start(bHandled);
	TestTrue(TEXT("played anyway: Play In Editor is asked"), bHandled);

	// Changed since it compiled (not compiled yet): compiled first, as Play does, and still broken
	FBlueprintEditorUtils::MarkBlueprintAsModified(Blueprint);
	TestTrue(TEXT("changed: compiled, and refused"), PlaytestBlueprintErrors::Describe(true).Contains(TEXT("must have a connection")));
	TestEqual(TEXT("compiled"), (int32)Blueprint->Status, (int32)BS_Error);

	// Fixed (the call taken out) and changed: compiled first, nothing stops the run
	FBlueprintEditorUtils::RemoveNode(Blueprint, Call, true);
	FBlueprintEditorUtils::MarkBlueprintAsModified(Blueprint);
	TestEqual(TEXT("fixed: nothing stops the run"), PlaytestBlueprintErrors::Describe(true), FString());
	TestTrue(TEXT("fixed: compiled"), Blueprint->Status == BS_UpToDate || Blueprint->Status == BS_UpToDateWithWarnings);
	Start(bHandled);
	TestTrue(TEXT("fixed: Play In Editor is asked"), bHandled);

	// (gone: no later Describe in this editor sees it)
	Blueprint->ClearFlags(RF_Public | RF_Standalone);
	Blueprint->MarkAsGarbage();
	Blueprint->GetPackage()->MarkAsGarbage();
	return true;
}

#endif
